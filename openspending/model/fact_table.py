import json
from itertools import count
from datetime import datetime

from sqlalchemy import MetaData
from sqlalchemy.schema import Table, Column
from sqlalchemy.types import Unicode, Integer, Date, Float
from sqlalchemy.sql.expression import select, func

from openspending.core import db
from openspending.lib.util import cache_hash
from openspending.model.dimension import DateDimension
from openspending.model.common import decode_row, df_column


TYPES = {
    'string': Unicode,
    'integer': Integer,
    'float': Float,
    'date': Date
}

DATE_FORMS = {
    'name': lambda value: value.isoformat(),
    'label': lambda value: value.strftime("%d. %B %Y"),
    'year': lambda value: value.strftime('%Y'),
    'quarter': lambda value: str(value.month / 4),
    'month': lambda value: value.strftime('%m'),
    'week': lambda value: value.strftime('%W'),
    'day': lambda value: value.strftime('%d')
}


class FactTable(object):
    """ The ``FactTable`` serves as a controller object for
    a given ``Model``, handling the creation, filling and migration
    of the table schema associated with the dataset. """
    
    def __init__(self, dataset):
        self.dataset = dataset
        
        self.bind = db.engine
        self.meta = MetaData()
        self.meta.bind = self.bind
        self._table = None
        
    @property
    def table(self):
        """ Generate an appropriate table representation to mirror the
        fields known for this table. """
        if self._table is None:
            name = '%s__facts' % self.dataset.name
            self._table = Table(name, self.meta)
            id_col = Column('_id', Unicode(42), primary_key=True)
            self._table.append_column(id_col)
            json_col = Column('_json', Unicode())
            self._table.append_column(json_col)
            self._fields_columns(self._table)
        return self._table

    @property
    def alias(self):
        """ An alias used for queries. """
        if not hasattr(self, '_alias'):
            self._alias = self.table.alias('entry')
        return self._alias

    @property
    def exists(self):
        return db.engine.has_table(self.table.name)

    def _fields_columns(self, table):
        """ Transform the (auto-detected) fields into a set of column
        specifications. """

        for name, field in self.dataset.fields.items():
            data_type = TYPES.get(field.get('type'), Unicode)
            col = Column(name, data_type, nullable=True)
            table.append_column(col)

            # Another date hack
            if field.get('type') == 'date':
                for k in DATE_FORMS.keys():
                    col = Column(df_column(name, k), Unicode, nullable=True)
                    table.append_column(col)

    def load_iter(self, iterable, chunk_size=1000):
        """ Bulk load all the data in an artifact to a matching database
        table. """
        chunk = []
        conn = self.bind.connect()
        tx = conn.begin()
        try:
            for record in iterable:
                chunk.append(self._expand_record(record))
                if len(chunk) >= chunk_size:
                    stmt = self.table.insert(chunk)
                    conn.execute(stmt)
                    chunk = []

            if len(chunk):
                stmt = self.table.insert(chunk)
                conn.execute(stmt)
            tx.commit()
        except:
            tx.rollback()
            raise

    def _expand_record(self, record):
        """ Transform an incoming record into a form that matches the
        fields schema. """

        for name, field in self.dataset.fields.items():
            v = record.get(name)

            # Another date hack
            if field.get('type') == 'date':
                for k, f in DATE_FORMS.items():
                    record[df_column(name, k)] = f(v)

        record['_id'] = cache_hash(record)
        record['_json'] = json.dumps(record)
        return record

    def create(self):
        """ Create the fact table if it does not exist. """
        if not self.exists:
            self.table.create(self.bind)

    def drop(self):
        """ Drop the fact table if it does exist. """
        if self.exists:
            self.table.drop()
        self._table = None

    def num_entries(self):
        """ Get the number of facts that are currently loaded. """
        if not self.exists:
            return 0
        rp = self.bind.execute(self.table.count())
        return rp.fetchone()[0]

    def entries(self, conditions="1=1", order_by=None, limit=None,
                offset=0, step=10000):
        """ Generate a fully denormalized view of the entries on this
        table. This view is nested so that each dimension will be a hash
        of its attributes. """
        if not self.exists:
            return

        selects = [self.alias.c._id]
        for axis in self.dataset.model.axes:

            # TODO: find a cleaner way to do this.
            if isinstance(axis, DateDimension):
                field = self.dataset.fields.get(axis.column, {})
                if field.get('type') == 'integer':
                    for k in ['name', 'label', 'year']:
                        label = df_column(axis.name, k)
                        selects.append(self.alias.c[axis.column].label(label))
                elif field.get('type') == 'date':
                    for k in DATE_FORMS:
                        k = df_column(axis.name, k)
                        if k in self.alias.columns:
                            selects.append(self.alias.c[k])

            else:
                for column in axis.columns:
                    if column is None or column not in self.alias.columns:
                        continue
                    selects.append(self.alias.c[column])

        # enforce stable sorting:
        if order_by is None:
            order_by = [self.alias.c._id.asc()]

        for i in count():
            qoffset = offset + (step * i)
            qlimit = step
            if limit is not None:
                qlimit = min(limit - (step * i), step)
            if qlimit <= 0:
                break

            query = select(selects, conditions, [], order_by=order_by,
                           use_labels=False, limit=qlimit, offset=qoffset)
            rp = self.bind.execute(query)

            first_row = True
            while True:
                row = rp.fetchone()
                if row is None:
                    if first_row:
                        return
                    break
                first_row = False
                yield decode_row(row, self.dataset.model)

    def timerange(self):
        """
        Get the timerange of the dataset (based on the time attribute).
        Returns a tuple of (first timestamp, last timestamp) where timestamp
        is a datetime object
        """
        if not self.exists or not self.dataset.model.exists:
            return (None, None)
    
        # Get the time column
        time = self.dataset.model['time']
        time = time.alias.c['name']
        # We use SQL's min and max functions to get the timestamps
        query = db.session.query(func.min(time), func.max(time))
        # We just need one result to get min and max time
        return [datetime.strptime(date, '%Y-%m-%d') if date else None
                for date in query.one()]

    def __repr__(self):
        return "<FactTable(%r)>" % (self.dataset)