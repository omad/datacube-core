# coding=utf-8
# We often have one-arg-per column, so these checks aren't so useful.
# pylint: disable=too-many-arguments,too-many-public-methods
"""
Lower-level database access.
"""
from __future__ import absolute_import

import datetime
import json
import logging
import re
from functools import reduce as reduce_

import numpy
from sqlalchemy import create_engine, select, text, bindparam, exists, and_, or_, Index, func, alias
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine.url import URL as EngineUrl
from sqlalchemy.exc import IntegrityError

import datacube
from datacube.config import LocalConfig
from datacube.index.fields import OrExpression
from . import tables
from ._fields import parse_fields, NativeField
from .tables import DATASET, DATASET_SOURCE, STORAGE_TYPE, METADATA_TYPE, DATASET_LOCATION, DATASET_TYPE

_LIB_ID = 'agdc-' + str(datacube.__version__)
APP_NAME_PATTERN = re.compile('^[a-zA-Z0-9-]+$')

DATASET_URI_FIELD = DATASET_LOCATION.c.uri_scheme + ':' + DATASET_LOCATION.c.uri_body
_DATASET_SELECT_FIELDS = (
    DATASET,
    # The most recent file uri. We may want more advanced path selection in the future...
    select([
        DATASET_URI_FIELD
    ]).where(
        and_(
            DATASET_LOCATION.c.dataset_ref == DATASET.c.id,
            DATASET_LOCATION.c.uri_scheme == 'file'
        )
    ).order_by(
        DATASET_LOCATION.c.added.desc()
    ).limit(1).label('local_uri')
)

PGCODE_UNIQUE_CONSTRAINT = '23505'

_LOG = logging.getLogger(__name__)


def _split_uri(uri):
    """
    Split the scheme and the remainder of the URI.

    >>> _split_uri('http://test.com/something.txt')
    ('http', '//test.com/something.txt')
    >>> _split_uri('eods:LS7_ETM_SYS_P31_GALPGS01-002_101_065_20160127')
    ('eods', 'LS7_ETM_SYS_P31_GALPGS01-002_101_065_20160127')
    >>> _split_uri('file://rhe-test-dev.prod.lan/data/fromASA/LANDSAT-7.89274.S4A2C1D3R3')
    ('file', '//rhe-test-dev.prod.lan/data/fromASA/LANDSAT-7.89274.S4A2C1D3R3')
    """
    comp = uri.split(':')
    scheme = comp[0]
    body = ':'.join(comp[1:])
    return scheme, body


class PostgresDb(object):
    """
    A very thin database access api.

    It exists so that higher level modules are not tied to SQLAlchemy, connections or specifics of database-access.

    (and can be unit tested without any actual databases)
    """

    def __init__(self, engine, connection):
        self._engine = engine
        self._connection = connection

    @classmethod
    def connect(cls, hostname, database, username=None, password=None, port=None, application_name=None):
        _engine = create_engine(
            EngineUrl(
                'postgresql',
                host=hostname, database=database, port=port,
                username=username, password=password,
            ),
            echo=False,
            # 'AUTOCOMMIT' here means READ-COMMITTED isolation level with autocommit on.
            # When a transaction is needed we will do an explicit begin/commit.
            isolation_level='AUTOCOMMIT',

            json_serializer=_to_json,
            connect_args={'application_name': application_name}
        )
        _connection = _engine.connect()
        return PostgresDb(_engine, _connection)

    @classmethod
    def from_config(cls, config=LocalConfig.find(), application_name=None):
        app_name = cls._expand_app_name(application_name)

        return PostgresDb.connect(
            config.db_hostname,
            config.db_database,
            config.db_username,
            config.db_password,
            config.db_port,
            application_name=app_name
        )

    @classmethod
    def _expand_app_name(cls, application_name):
        """
        >>> PostgresDb._expand_app_name(None) #doctest: +ELLIPSIS
        'agdc-...'
        >>> PostgresDb._expand_app_name('cli') #doctest: +ELLIPSIS
        'cli agdc-...'
        >>> PostgresDb._expand_app_name('not valid')
        Traceback (most recent call last):
        ...
        ValueError: Invalid application name 'not valid': Must be alphanumeric with dashes.
        >>> PostgresDb._expand_app_name('') #doctest: +ELLIPSIS
        Traceback (most recent call last):
        ...
        ValueError: Invalid application name '': Must be alphanumeric with dashes.
        """
        full_name = _LIB_ID
        if application_name is not None:
            if not APP_NAME_PATTERN.match(application_name):
                raise ValueError('Invalid application name %r: Must be alphanumeric with dashes.' % application_name)

            full_name = application_name + ' ' + _LIB_ID

        if len(full_name) > 64:
            raise ValueError('Application name is too long: Maximum %s chars' % (64 - len(_LIB_ID)))
        return full_name

    def init(self, with_permissions=True):
        """
        Init a new database (if not already set up).

        :return: If it was newly created.
        """
        return tables.ensure_db(self._engine, with_permissions=with_permissions)

    def begin(self):
        """
        Start a transaction.

        Returns a transaction object. Call commit() or rollback() to complete the
        transaction or use a context manager:

            with db.begin() as transaction:
                db.insert_dataset(...)

        :return: Tranasction object
        """
        return _BegunTransaction(self._connection)

    def insert_dataset(self, metadata_doc, dataset_id, dataset_type_id=None, storage_type_id=None):
        """
        Insert dataset if not already indexed.
        :type metadata_doc: dict
        :type dataset_id: str or uuid.UUID
        :type dataset_type_id: int
        :return: whether it was inserted
        :rtype: bool
        """
        if dataset_type_id is None:
            d_type_result = self.determine_dataset_type_for_doc(metadata_doc)
            if not d_type_result:
                _LOG.debug('Attempted failed match on doc %r', metadata_doc)
                raise RuntimeError('No dataset type matches dataset')
            dataset_type_id = d_type_result['id']
            _LOG.debug('Matched collection %r', dataset_type_id)
        else:
            _LOG.debug('Using provided collection %r', dataset_type_id)

        try:
            dataset_type_ref = bindparam('dataset_type_ref')
            ret = self._connection.execute(
                # Insert if not exists.
                #     (there's still a tiny chance of a race condition: It will throw an integrity error if another
                #      connection inserts the same dataset in the time between the subquery and the main query.
                #      This is ok for our purposes.)
                DATASET.insert().from_select(
                    ['id', 'dataset_type_ref', 'storage_type_ref', 'metadata_type_ref', 'metadata'],
                    select([
                        bindparam('id'), dataset_type_ref, storage_type_id,
                        select([
                            DATASET_TYPE.c.metadata_type_ref
                        ]).where(
                            DATASET_TYPE.c.id == dataset_type_ref
                        ).label('metadata_type_ref'),
                        bindparam('metadata', type_=JSONB)
                    ]).where(~exists(select([DATASET.c.id]).where(DATASET.c.id == bindparam('id'))))
                ),
                id=dataset_id,
                dataset_type_ref=dataset_type_id,
                metadata=metadata_doc
            )
            return ret.rowcount > 0
        except IntegrityError as e:
            if e.orig.pgcode == PGCODE_UNIQUE_CONSTRAINT:
                _LOG.info('Duplicate dataset, not inserting: %s', dataset_id)
                # We're still going to raise it, because the transaction will have been invalidated.
            raise

    def ensure_dataset_location(self, dataset_id, uri):
        """
        Add a location to a dataset if it is not already recorded.
        :type dataset_id: str or uuid.UUID
        :type uri: str
        """
        scheme, body = _split_uri(uri)
        # Insert if not exists.
        #     (there's still a tiny chance of a race condition: It will throw an integrity error if another
        #      connection inserts the same location in the time between the subquery and the main query.
        #      This is ok for our purposes.)
        self._connection.execute(
            DATASET_LOCATION.insert().from_select(
                ['dataset_ref', 'uri_scheme', 'uri_body'],
                select([
                    bindparam('dataset_ref'), bindparam('uri_scheme'), bindparam('uri_body'),
                ]).where(
                    ~exists(select([DATASET_LOCATION.c.id]).where(
                        and_(
                            DATASET_LOCATION.c.dataset_ref == bindparam('dataset_ref'),
                            DATASET_LOCATION.c.uri_scheme == bindparam('uri_scheme'),
                            DATASET_LOCATION.c.uri_body == bindparam('uri_body'),
                        ),
                    ))
                )
            ),
            dataset_ref=dataset_id,
            uri_scheme=scheme,
            uri_body=body,
        )

    def contains_dataset(self, dataset_id):
        return bool(self._connection.execute(select([DATASET.c.id]).where(DATASET.c.id == dataset_id)).fetchone())

    def insert_dataset_source(self, classifier, dataset_id, source_dataset_id):
        res = self._connection.execute(
            DATASET_SOURCE.insert(),
            classifier=classifier,
            dataset_ref=dataset_id,
            source_dataset_ref=source_dataset_id
        )
        return res.inserted_primary_key[0]

    def get_storage_type(self, storage_type_id):
        return self._connection.execute(
            STORAGE_TYPE.select().where(STORAGE_TYPE.c.id == storage_type_id)
        ).first()

    def get_dataset(self, dataset_id):
        return self._connection.execute(
            select(_DATASET_SELECT_FIELDS).where(DATASET.c.id == dataset_id)
        ).first()

    def get_dataset_sources(self, dataset_id):
        # recursively build the list of (dataset_ref, source_dataset_ref) pairs starting from dataset_id
        # include (dataset_ref, NULL) [hence the left join]
        sources = select(
            [DATASET_SOURCE.c.dataset_ref,
             DATASET_SOURCE.c.source_dataset_ref,
             DATASET_SOURCE.c.classifier]
        ).where(
            DATASET_SOURCE.c.dataset_ref == dataset_id
        ).cte(name="sources", recursive=True)

        sources = sources.union_all(
            select(
                [sources.c.source_dataset_ref.label('dataset_ref'),
                 DATASET_SOURCE.c.source_dataset_ref,
                 DATASET_SOURCE.c.classifier]
            ).select_from(
                sources.join(DATASET_SOURCE,
                             sources.c.source_dataset_ref == DATASET_SOURCE.c.dataset_ref,
                             isouter=True)
            ).where(sources.c.source_dataset_ref != None))

        # turn the list of pairs into adjacency list (dataset_ref, [source_dataset_ref, ...])
        # some source_dataset_ref's will be NULL
        aggd = select(
            [sources.c.dataset_ref,
             func.array_agg(sources.c.source_dataset_ref).label('sources'),
             func.array_agg(sources.c.classifier).label('classes')]
        ).group_by(sources.c.dataset_ref).alias('aggd')

        # join the adjacency list with datasets table
        query = select(
            _DATASET_SELECT_FIELDS + (aggd.c.sources, aggd.c.classes)
        ).select_from(aggd.join(DATASET, DATASET.c.id == aggd.c.dataset_ref))

        return self._connection.execute(query).fetchall()

    def get_storage_types(self, dataset_metadata):
        """
        Find any storage types that match the given dataset.

        :type dataset_metadata: dict
        :rtype: dict
        """
        # Find any storage types whose 'dataset_metadata' document is a subset of the metadata.
        return self._connection.execute(
            STORAGE_TYPE.select().where(
                STORAGE_TYPE.c.dataset_metadata.contained_by(dataset_metadata)
            )
        ).fetchall()

    def get_all_storage_types(self):
        return self._connection.execute(
            STORAGE_TYPE.select()
        ).fetchall()

    def ensure_storage_type(self,
                            name,
                            dataset_metadata,
                            definition):
        res = self._connection.execute(
            STORAGE_TYPE.insert().values(
                name=name,
                dataset_metadata=dataset_metadata,
                definition=definition
            )
        )
        storage_type_id = res.inserted_primary_key[0]
        cube_sql_str = self._storage_unit_cube_sql_str(definition['storage']['dimension_order'])
        constraint = """alter table agdc.storage_unit add exclude using gist (%s with &&)
                        where (storage_type_ref = %s)""" % (cube_sql_str, storage_type_id)
        # TODO: must enforce cube extension somehow before we can do this
        # self._connection.execute(constraint)
        return storage_type_id

    def archive_storage_unit(self, storage_unit_id):
        self._connection.execute(
            DATASET.update()
                .where(DATASET.c.id == storage_unit_id)
                .where(DATASET.c.archived == None)
                .values(archived=func.now())
        )

    def _storage_unit_cube_sql_str(self, dimensions):
        def _array_str(p):
            return 'ARRAY[' + ','.join("CAST(descriptor #>> '{coordinates,%s,%s}' as numeric)" % (c, p)
                                       for c in dimensions) + ']'

        return "cube(" + ','.join(_array_str(p) for p in ['begin', 'end']) + ")"

    def get_storage_unit_overlap(self, storage_type):
        # TODO: This is probably totally broken after the storage_unit->dataset unification. But all its tests pass!
        wild_sql_appears = self._storage_unit_cube_sql_str(storage_type.dimensions) + ' as cube'
        su1 = select([
            DATASET.c.id,
            text(wild_sql_appears)
        ]).where(DATASET.c.dataset_type_ref == storage_type.id)
        su1 = alias(su1, name='su1')
        su2 = alias(su1, name='su2')

        overlaps = select([su1.c.id]).where(
            exists(
                select([1]).select_from(su2).where(
                    and_(
                        su1.c.id != su2.c.id,
                        text("su1.cube && su2.cube")
                    )
                )
            )
        )

        return self._connection.execute(overlaps).fetchall()

    def get_dataset_fields(self, collection_result):
        # Native fields (hard-coded into the schema)
        fields = {
            'id': NativeField(
                'id',
                None,
                None,
                DATASET.c.id
            ),
            # 'collection': NativeField(
            #     'collection',
            #     'Name of collection',
            #     None, COLLECTION.c.name
            # )
        }
        dataset_search_fields = collection_result['definition']['dataset']['search_fields']

        # noinspection PyTypeChecker
        fields.update(
            parse_fields(
                dataset_search_fields,
                collection_result['id'],
                DATASET.c.metadata
            )
        )
        return fields

    def search_datasets_by_metadata(self, metadata):
        """
        Find any datasets that have the given metadata.

        :type metadata: dict
        :rtype: dict
        """
        # Find any storage types whose 'dataset_metadata' document is a subset of the metadata.
        return self._connection.execute(
            select(_DATASET_SELECT_FIELDS).where(DATASET.c.metadata.contains(metadata))
        ).fetchall()

    def search_datasets(self, expressions, select_fields=None, with_source_ids=False):
        """
        :type with_source_ids: bool
        :type select_fields: tuple[datacube.index.postgres._fields.PgField]
        :type expressions: tuple[datacube.index.postgres._fields.PgExpression]
        :rtype: dict
        """
        select_fields = tuple(
            f.alchemy_expression.label(f.name)
            for f in select_fields
        ) if select_fields else _DATASET_SELECT_FIELDS

        if with_source_ids:
            # Include the IDs of source datasets
            select_fields + (
                func.array_agg(
                    DATASET_SOURCE
                        .select(DATASET_SOURCE.c.source_dataset_ref)
                        .where(DATASET_SOURCE.c.dataset_ref == DATASET.c.id)
                ).label('dataset_refs')
            )

        return self._search_docs(
            expressions,
            primary_table=DATASET,
            select_fields=select_fields,
        )

    def _search_docs(self, expressions, primary_table, select_fields=None, group_by_fields=None, required_tables=None):
        """

        :type expressions: tuple[datacube.index.postgres._fields.PgExpression]
        :param primary_table: SQLAlchemy table
        :return:
        """
        from_expression, raw_expressions = _prepare_expressions(
            expressions, primary_table,
            required_tables=required_tables
        )

        select_query = (
            select(select_fields)
                .select_from(from_expression)
                .where(and_(DATASET.c.archived == None, *raw_expressions)))

        if group_by_fields:
            select_query = select_query.group_by(*group_by_fields)

        results = self._connection.execute(select_query)
        for result in results:
            yield result

    def determine_dataset_type_for_doc(self, metadata_doc):
        """
        :type metadata_doc: dict
        :rtype: dict or None
        """
        matching_types = self._connection.execute(
            DATASET_TYPE.select().where(
                DATASET_TYPE.c.metadata.contained_by(metadata_doc)
            )
        ).fetchall()

        if len(matching_types) > 1:
            pass

    def get_dataset_type(self, id_):
        return self._connection.execute(
            DATASET_TYPE.select().where(DATASET_TYPE.c.id == id_)
        ).first()

    def get_metadata_type(self, id_):
        return self._connection.execute(
            METADATA_TYPE.select().where(METADATA_TYPE.c.id == id_)
        ).first()

    def get_dataset_type_by_name(self, name):
        return self._connection.execute(
            DATASET_TYPE.select().where(DATASET_TYPE.c.name == name)
        ).first()

    def get_metadata_type_by_name(self, name):
        return self._connection.execute(
            METADATA_TYPE.select().where(METADATA_TYPE.c.name == name)
        ).first()

    def get_storage_type_by_name(self, name):
        return self._connection.execute(
            STORAGE_TYPE.select().where(STORAGE_TYPE.c.name == name)
        ).first()

    def add_dataset_type(self,
                         name,
                         metadata,
                         metadata_type_id,
                         definition):
        res = self._connection.execute(
            DATASET_TYPE.insert().values(
                name=name,
                metadata=metadata,
                metadata_type_ref=metadata_type_id,
                definition=definition
            )
        )
        return res.inserted_primary_key[0]

    def add_metadata_type(self, name, definition, concurrently=False):
        res = self._connection.execute(
            METADATA_TYPE.insert().values(
                name=name,
                definition=definition
            )
        )
        type_id = res.inserted_primary_key[0]
        record = self.get_metadata_type(type_id)

        # Initialise search fields.
        _setup_collection_fields(
            self._connection, name, 'dataset', self.get_dataset_fields(record),
            and_(DATASET.c.metadata_type_ref == type_id, DATASET.c.archived == None),
            concurrently=concurrently
        )

    def get_all_dataset_types(self):
        return self._connection.execute(DATASET_TYPE.select()).fetchall()

    def count_storage_types(self):
        return self._connection.execute(select([func.count()]).select_from(STORAGE_TYPE)).scalar()

    def get_locations(self, dataset_id):
        return [
            record[0]
            for record in self._connection.execute(
                select([
                    DATASET_URI_FIELD
                ]).where(
                    DATASET_LOCATION.c.dataset_ref == dataset_id
                ).order_by(
                    DATASET_LOCATION.c.added.desc()
                )
            ).fetchall()
            ]

    def __repr__(self):
        return "PostgresDb<engine={!r}>".format(self._engine)

    def list_users(self):
        result = self._connection.execute("""
            select
                group_role.rolname as role_name,
                user_role.rolname as user_name,
                pg_catalog.shobj_description(user_role.oid, 'pg_authid') as description
            from pg_roles group_role
            inner join pg_auth_members am on am.roleid = group_role.oid
            inner join pg_roles user_role on am.member = user_role.oid
            where (group_role.rolname like 'agdc_%%') and not (user_role.rolname like 'agdc_%%')
            order by group_role.oid asc, user_role.oid asc;
        """)
        for row in result:
            yield _from_pg_role(row['role_name']), row['user_name'], row['description']

    def create_user(self, username, key, role):
        pg_role = _to_pg_role(role)
        tables.create_user(self._engine, username, key, pg_role)

    def grant_role(self, role, users):
        """
        Grant a role to a user.
        """
        pg_role = _to_pg_role(role)

        for user in users:
            if not tables.has_role(self._engine, user):
                raise ValueError('Unknown user %r' % user)

        tables.grant_role(self._engine, pg_role, users)


def _to_pg_role(role):
    """
    >>> _to_pg_role('ingest')
    'agdc_ingest'
    >>> _to_pg_role('fake')
    Traceback (most recent call last):
    ...
    ValueError: Unknown role 'fake'. Expected one of ...
    """
    pg_role = 'agdc_' + role.lower()
    if pg_role not in tables.USER_ROLES:
        raise ValueError(
            'Unknown role %r. Expected one of %r' %
            (role, [r.split('_')[1] for r in tables.USER_ROLES])
        )
    return pg_role


def _from_pg_role(pg_role):
    """
    >>> _from_pg_role('agdc_admin')
    'admin'
    >>> _from_pg_role('fake')
    Traceback (most recent call last):
    ...
    ValueError: Not a pg role: 'fake'. Expected one of ...
    """
    if pg_role not in tables.USER_ROLES:
        raise ValueError('Not a pg role: %r. Expected one of %r' % (pg_role, tables.USER_ROLES))

    return pg_role.split('_')[1]


def _pg_exists(conn, name):
    """
    Does a postgres object exist?
    :rtype bool
    """
    return conn.execute("SELECT to_regclass(%s)", name).scalar() is not None


def _setup_collection_fields(conn, collection_prefix, doc_prefix, fields, where_expression, concurrently=False):
    """
    Create indexes and views for a collection's search fields.
    """
    name = '{}_{}'.format(collection_prefix.lower(), doc_prefix.lower())

    # Create indexes for the search fields.
    for field in fields.values():
        index_type = field.postgres_index_type
        if index_type:
            # Our normal indexes start with "ix_", dynamic indexes with "dix_"
            index_name = 'dix_field_{prefix}_{field_name}'.format(
                prefix=name.lower(),
                field_name=field.name.lower()
            )
            _LOG.debug('Creating index: %s', index_name)

            if not _pg_exists(conn, tables.schema_qualified(index_name)):
                Index(
                    index_name,
                    field.alchemy_expression,
                    postgresql_where=where_expression,
                    postgresql_using=index_type,
                    # Don't lock the table (in the future we'll allow indexing new fields...)
                    postgresql_concurrently=concurrently
                ).create(conn)

    # Create a view of search fields (for debugging convenience).
    view_name = tables.schema_qualified(name)
    if not _pg_exists(conn, view_name):
        conn.execute(
            tables.View(
                view_name,
                select(
                    [field.alchemy_expression.label(field.name) for field in fields.values()]
                ).where(where_expression)
            )
        )


def _prepare_expressions(expressions, primary_table, required_tables=None):
    """
    :type expressions: tuple[datacube.index.postgres._fields.PgExpression]
    :param primary_table: SQLAlchemy table
    """
    # We currently only allow one metadata to be queried at a time (our indexes are per-type)
    metadata_type_references = set()
    join_tables = set(required_tables) if required_tables else set()

    def tables_referenced(expression):
        if isinstance(expression, OrExpression):
            return reduce_(lambda a, b: a | b, (tables_referenced(expr) for expr in expression.exprs), set())

        #: :type: datacube.index.postgres._fields.PgField
        field = expression.field
        table = field.alchemy_column.table
        metadata_type_id = field.metadata_type_id
        return {(table, metadata_type_id)}

    for table, metadata_type_id in reduce_(lambda a, b: a | b, (tables_referenced(expr) for expr in expressions),
                                           set()):
        if table != primary_table:
            join_tables.add(table)
        if metadata_type_id:
            metadata_type_references.add((table, metadata_type_id))

    unique_metadata_types = set([c[1] for c in metadata_type_references])
    if len(unique_metadata_types) > 1:
        raise ValueError(
            'Currently only one metadata type can be queried at a time. (Tried %r)' % metadata_type_references
        )

    def raw_expr(expression):
        if isinstance(expression, OrExpression):
            return or_(raw_expr(expr) for expr in expression.exprs)
        return expression.alchemy_expression

    raw_expressions = [raw_expr(expression) for expression in expressions]

    # We may have multiple references: storage.metadata_type_ref and dataset.metadata_type_ref.
    # We want to include all, to ensure the indexes are used.
    for from_table, queried_metadata_type in metadata_type_references:
        raw_expressions.insert(0, from_table.c.metadata_type_ref == queried_metadata_type)

    from_expression = _prepare_from_expression(primary_table, join_tables)

    return from_expression, raw_expressions


def _prepare_from_expression(primary_table, join_tables):
    """
    Calculate an SQLAlchemy from expression to join the given table to other required tables.
    """
    from_expression = primary_table
    for table in join_tables:
        from_expression = from_expression.join(table)
    return from_expression


def transform_object_tree(o, f):
    if isinstance(o, dict):
        return {k: transform_object_tree(v, f) for k, v in o.items()}
    if isinstance(o, list):
        return [transform_object_tree(v, f) for v in o]
    if isinstance(o, tuple):
        return tuple(transform_object_tree(v, f) for v in o)
    return f(o)


def _to_json(o):
    # Postgres <=9.5 doesn't support NaN and Infinity
    def fixup_value(v):
        if isinstance(v, float):
            if v != v:
                return "NaN"
            if v == float("inf"):
                return "Infinity"
            if v == float("-inf"):
                return "-Infinity"
        if isinstance(v, (datetime.datetime, datetime.date)):
            return v.isoformat()
        if isinstance(v, numpy.dtype):
            return v.name
        return v

    fixedup = transform_object_tree(o, fixup_value)

    return json.dumps(fixedup, default=_json_fallback)


def _json_fallback(obj):
    """Fallback json serialiser."""
    raise TypeError("Type not serializable: {}".format(type(obj)))


class _BegunTransaction(object):
    def __init__(self, connection):
        self._connection = connection
        self.begin()

    def begin(self):
        self._connection.execute(text('BEGIN'))

    def commit(self):
        self._connection.execute(text('COMMIT'))

    def rollback(self):
        self._connection.execute(text('ROLLBACK'))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
