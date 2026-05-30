# Obele Library Walkthrough

This document provides a detailed walkthrough of the `obele` library, explaining its core architecture, how its components fit together, and an exhaustive API reference.

## Architectural Overview: How It All Fits Together

`obele` is a lightweight, async-ready ORM (Object-Relational Mapper) and Key-Value (KV) store built specifically for SQLite. It is designed to be highly concurrent for reads while safely serializing writes, making it ideal for modern Python applications using `asyncio` or multithreading.

The library is broadly divided into two main components:
1. **The ORM (`obele.orm`)**: For defining structured data models and querying them using Python classes.
2. **The KV Store (`obele.kv`)**: For schema-less, fast key-value storage.

### 1. Database Connection Management (`Database`)
At the core of `obele` is the `Database` class (in `obele.orm.database`). This acts as a thread-safe connection pool manager.
- **Concurrency**: It creates a unique SQLite connection per thread, enabling genuine concurrent reads (especially when SQLite is in WAL mode). Write operations are serialized using global thread locks.
- **Async & Sync**: Every method in the library generally has a synchronous version (e.g., `execute`) and an asynchronous version prefixed with `a` (e.g., `aexecute`).
- **Scoping**: You can configure a global database using `Database.configure("my_db.sqlite3")`, or use thread-local temporary databases with the `Database.using()` context manager.

### 2. Modeling Data (`Model`, `Field`, `MetaModel`)
You define database tables by subclassing the `Model` class (in `obele.orm.model`). 
- **`Model`**: The base class for mapping Python objects to SQLite rows. It tracks state changes (using dirty tracking) to optimize `UPDATE` queries.
- **`Field`**: Subclasses of `Field` (like `TextField`, `IntegerField`, `ForeignKeyField`) define the schema. These are Python descriptors that handle validation, Python-to-SQLite type conversion, and table DDL generation.
- **`MetaModel`**: The metaclass that intercepts the creation of your `Model` subclasses, gathers the fields, configures the primary key, pre-compiles SQL templates for fast insertion/updates, and automatically wires up reverse relationships for foreign keys.

### 3. Querying (`QuerySet` and Query Expressions)
When you want to retrieve records, you use the `filter()`, `exclude()`, or `all()` methods on a `Model`, which return a `QuerySet` (in `obele.orm.query`).
- **`QuerySet`**: A lazy, chainable query builder. It doesn't execute SQL until you iterate over it or call a terminal method like `all()`, `first()`, `count()`, or their async equivalents.
- **Expressions**: You can build complex queries using `Q` objects (for complex WHERE clauses with AND/OR), `F` objects (to reference columns dynamically), `Func` (for SQLite functions like LOWER or JSON_EXTRACT), and aggregations (`Count`, `Sum`, etc.).

### 4. Utilities (Pagination, Search, Mixins, Signals)
To make app development easier, the ORM includes:
- **Signals**: A lightweight pub/sub system (`pre_save`, `post_save`, etc.) to hook into the model lifecycle.
- **Pagination**: `Page` for offset-based pagination and `CursorPage` for fast, cursor-based pagination.
- **Mixins**: Common patterns like `TimestampMixin` (for `created_at`/`updated_at`) and `SoftDeleteMixin` (for safe deletion).
- **Search**: `SearchIndex` sets up SQLite FTS5 (Full Text Search) virtual tables seamlessly tied to your models.

### 5. Key-Value Store (`KVStore`)
In `obele.kv.store`, `KVStore` uses a dedicated SQLite table to provide a simple dictionary-like interface for arbitrary data. It supports JSON serialization under the hood, namespace isolation, caching, and TTL (Time-To-Live) expiration.

---

# Obele Library API Reference

## Module: `obele.orm.database`

Thread-safe SQLite connection manager with sync, async, and scoped APIs.

Uses per-thread connections via ``threading.local()`` for genuine read
concurrency under WAL journal mode, while write operations are serialized
through a single global lock.


### Class `Database`

Thread-safe SQLite connection manager with sync and async APIs.

Uses per-thread connections for concurrent reads and a global write lock
for serialized writes.  The configured database remains available globally,
but callers can open a temporary scoped binding via :meth:`using`.

Configuration options::

    Database.configure(
        "app.db",
        pragmas={"cache_size": -16000},
        pool_size=10,            # max connections in the pool
        log_queries=True,        # log all SQL to the 'obele' logger
        slow_query_threshold=0.5, # log queries slower than 500ms as warnings
    )


#### Methods

- **`__aenter__(self) -> 'Database'`**

- **`__aexit__(self, exc_type: 'type | None', exc_val: 'BaseException | None', exc_tb: 'Any') -> 'None'`**

- **`__enter__(self) -> 'Database'`**

- **`__exit__(self, exc_type: 'type | None', exc_val: 'BaseException | None', exc_tb: 'Any') -> 'None'`**

### Class `_DatabaseScope`

Context manager for temporary scoped database bindings.

#### Methods

- **`__aenter__(self) -> 'type[Database]'`**

- **`__aexit__(self, exc_type: 'type | None', exc_val: 'BaseException | None', exc_tb: 'Any') -> 'None'`**

- **`__enter__(self) -> 'type[Database]'`**

- **`__exit__(self, exc_type: 'type | None', exc_val: 'BaseException | None', exc_tb: 'Any') -> 'None'`**

- **`__init__(self, database_cls: 'type[Database]', db_path: 'str', pragmas: 'dict[str, Any] | None' = None) -> 'None'`**

### Class `_DatabaseTransaction`

Context manager for explicit transactions with savepoint nesting.

#### Methods

- **`__aenter__(self) -> 'sqlite3.Connection'`**

- **`__aexit__(self, exc_type: 'type | None', exc_val: 'BaseException | None', exc_tb: 'Any') -> 'None'`**

- **`__enter__(self) -> 'sqlite3.Connection'`**

- **`__exit__(self, exc_type: 'type | None', exc_val: 'BaseException | None', exc_tb: 'Any') -> 'None'`**

- **`__init__(self, database_cls: 'type[Database]') -> 'None'`**

### Class `_ScopedBinding`

_ScopedBinding(db_path: 'str', pragmas: 'dict[str, Any]', connection: 'sqlite3.Connection | None' = None)

#### Methods

- **`__init__(self, db_path: 'str', pragmas: 'dict[str, Any]', connection: 'sqlite3.Connection | None' = None) -> None`**

## Module: `obele.orm.model`

obele model base class and metaclass with sync and async APIs.

Provides the ``Model`` class that maps Python classes to SQLite tables with
full CRUD support, schema management, and query-builder integration.

Async methods are prefixed with ``a`` (e.g. ``acreate``, ``asave``)::

    # Sync
    users = User.filter(age__gt=18).order_by("-name").limit(10).all()
    # Async
    users = await User.filter(age__gt=18).order_by("-name").limit(10).aall()


### Class `MetaModel`

Collect ``Field`` descriptors and configure the Model class.

#### Methods

### Class `Model`

Base class for all ORM models.

Subclass and declare ``Field`` class attributes to define the schema::

    class User(Model):
        table_name = "users"
        name = TextField()
        age  = IntegerField(nullable=True)

    User.create_table()
    alice = User.create(name="Alice", age=30)
    alice.name = "Alicia"
    alice.save()


#### Methods

- **`__init__(self, **kwargs: 'Any') -> 'None'`**

- **`adelete(self) -> 'None'`**
  > Async version of :meth:`delete`.

- **`arefresh(self) -> 'None'`**
  > Async version of :meth:`refresh`.

- **`asave(self) -> 'None'`**
  > Async version of :meth:`save`.

- **`delete(self) -> 'None'`**
  > Delete this instance from the database.

- **`refresh(self) -> 'None'`**
  > Re-read this instance's data from the database.

- **`save(self) -> 'None'`**
  > Insert or update this instance in the database.

- **`to_db_dict(self, *, include_annotations: 'bool' = True) -> 'dict[str, Any]'`**
  > Shorthand for ``to_dict(mode="db")``.

- **`to_dict(self, *, mode: 'str' = 'python', include_annotations: 'bool' = True) -> 'dict[str, Any]'`**
  > Serialize all fields to a dictionary.

### Class `ReverseRelationDescriptor`

Descriptor installed on a target Model by the metaclass.

On attribute access it returns a :class:`ReverseRelationManager`
bound to the calling instance.


#### Methods

- **`__init__(self, related_model: 'type[Model]', field_name: 'str', accessor_name: 'str') -> 'None'`**

### Class `ReverseRelationManager`

Query helper for reverse foreign-key access.

Returned by the ``ReverseRelationDescriptor``; supports the full
QuerySet API plus convenience helpers like ``create()``::

    user.posts.all()
    user.posts.filter(published=True).count()
    user.posts.create(title="New Post")


#### Methods

- **`__init__(self, instance: 'Model', related_model: 'type[Model]', field_name: 'str') -> 'None'`**

- **`acreate(self, **kwargs: 'Any') -> 'Model'`**
  > Async version of :meth:`create`.

- **`create(self, **kwargs: 'Any') -> 'Model'`**
  > Create a related object with the FK pre-set to this instance.

## Module: `obele.orm.fields`

Typed field descriptors for the ORM.

Each field maps a Python type to a SQLite column type, with support for
validation, serialization, and column-level constraints.


### Class `BlobField`

#### Methods

- **`__init__(self, *, primary_key: 'bool' = False, nullable: 'bool' = False, default: 'Any' = <object object at 0x0000020275050B10>, db_default: 'str | None' = None, unique: 'bool' = False, index: 'bool' = False, column_name: 'str' = '', validators: 'Sequence[Callable[[Any], None]] | None' = None, check: 'str | None' = None)`**

- **`column_ddl(self) -> 'str'`**
  > Return the full column-definition fragment for CREATE TABLE.

- **`to_db(self, value: 'Any') -> 'Any'`**
  > Convert a Python value to something SQLite can store.

- **`to_python(self, value: 'Any') -> 'Any'`**
  > Convert *value* coming from the database (or user) to the Python type.

- **`validate(self, value: 'Any') -> 'None'`**
  > Validate *value* against this field's constraints.

### Class `BooleanField`

Stored as 0/1 in SQLite, exposed as ``bool`` in Python.

#### Methods

- **`__init__(self, *, primary_key: 'bool' = False, nullable: 'bool' = False, default: 'Any' = <object object at 0x0000020275050B10>, db_default: 'str | None' = None, unique: 'bool' = False, index: 'bool' = False, column_name: 'str' = '', validators: 'Sequence[Callable[[Any], None]] | None' = None, check: 'str | None' = None)`**

- **`column_ddl(self) -> 'str'`**
  > Return the full column-definition fragment for CREATE TABLE.

- **`to_db(self, value: 'Any') -> 'int | None'`**

- **`to_python(self, value: 'Any') -> 'bool'`**

- **`validate(self, value: 'Any') -> 'None'`**
  > Validate *value* against this field's constraints.

### Class `DateField`

Stored as ISO-8601 TEXT (date only) in SQLite, exposed as ``datetime.date``.

#### Methods

- **`__init__(self, *, primary_key: 'bool' = False, nullable: 'bool' = False, default: 'Any' = <object object at 0x0000020275050B10>, db_default: 'str | None' = None, unique: 'bool' = False, index: 'bool' = False, column_name: 'str' = '', validators: 'Sequence[Callable[[Any], None]] | None' = None, check: 'str | None' = None)`**

- **`column_ddl(self) -> 'str'`**
  > Return the full column-definition fragment for CREATE TABLE.

- **`to_db(self, value: 'Any') -> 'str | None'`**

- **`to_python(self, value: 'Any') -> 'datetime.date'`**

- **`validate(self, value: 'Any') -> 'None'`**
  > Validate *value* against this field's constraints.

### Class `DateTimeField`

Stored as ISO-8601 TEXT in SQLite, exposed as ``datetime.datetime``.

#### Methods

- **`__init__(self, *, primary_key: 'bool' = False, nullable: 'bool' = False, default: 'Any' = <object object at 0x0000020275050B10>, db_default: 'str | None' = None, unique: 'bool' = False, index: 'bool' = False, column_name: 'str' = '', validators: 'Sequence[Callable[[Any], None]] | None' = None, check: 'str | None' = None)`**

- **`column_ddl(self) -> 'str'`**
  > Return the full column-definition fragment for CREATE TABLE.

- **`to_db(self, value: 'Any') -> 'str | None'`**

- **`to_python(self, value: 'Any') -> 'datetime.datetime'`**

- **`validate(self, value: 'Any') -> 'None'`**
  > Validate *value* against this field's constraints.

### Class `DecimalField`

Stored as TEXT, exposed as :class:`decimal.Decimal`.

#### Methods

- **`__init__(self, *, primary_key: 'bool' = False, nullable: 'bool' = False, default: 'Any' = <object object at 0x0000020275050B10>, db_default: 'str | None' = None, unique: 'bool' = False, index: 'bool' = False, column_name: 'str' = '', validators: 'Sequence[Callable[[Any], None]] | None' = None, check: 'str | None' = None)`**

- **`column_ddl(self) -> 'str'`**
  > Return the full column-definition fragment for CREATE TABLE.

- **`to_db(self, value: 'Any') -> 'str | None'`**

- **`to_python(self, value: 'Any') -> 'decimal.Decimal'`**

- **`validate(self, value: 'Any') -> 'None'`**

### Class `EmailField`

A :class:`TextField` that validates email address format::

class User(Model):
    email = EmailField(unique=True, index=True)


#### Methods

- **`__init__(self, *, max_length: 'int' = 254, **kwargs: 'Any')`**

- **`column_ddl(self) -> 'str'`**
  > Return the full column-definition fragment for CREATE TABLE.

- **`to_db(self, value: 'Any') -> 'Any'`**
  > Convert a Python value to something SQLite can store.

- **`to_python(self, value: 'Any') -> 'Any'`**
  > Convert *value* coming from the database (or user) to the Python type.

- **`validate(self, value: 'Any') -> 'None'`**

### Class `EnumField`

Stored as TEXT in SQLite, exposed as a Python :class:`~enum.Enum`.

Usage::

    class Status(enum.Enum):
        DRAFT = "draft"
        PUBLISHED = "published"

    class Post(Model):
        status = EnumField(enum_class=Status, default=Status.DRAFT)


#### Methods

- **`__init__(self, *, enum_class: 'type[enum.Enum]', **kwargs: 'Any')`**

- **`column_ddl(self) -> 'str'`**
  > Return the full column-definition fragment for CREATE TABLE.

- **`to_db(self, value: 'Any') -> 'str | None'`**

- **`to_python(self, value: 'Any') -> 'enum.Enum'`**

- **`validate(self, value: 'Any') -> 'None'`**

### Class `Field`

Base descriptor for all ORM fields.

Subclasses must set ``sql_type`` and ``python_type``.


#### Methods

- **`__init__(self, *, primary_key: 'bool' = False, nullable: 'bool' = False, default: 'Any' = <object object at 0x0000020275050B10>, db_default: 'str | None' = None, unique: 'bool' = False, index: 'bool' = False, column_name: 'str' = '', validators: 'Sequence[Callable[[Any], None]] | None' = None, check: 'str | None' = None)`**

- **`column_ddl(self) -> 'str'`**
  > Return the full column-definition fragment for CREATE TABLE.

- **`to_db(self, value: 'Any') -> 'Any'`**
  > Convert a Python value to something SQLite can store.

- **`to_python(self, value: 'Any') -> 'Any'`**
  > Convert *value* coming from the database (or user) to the Python type.

- **`validate(self, value: 'Any') -> 'None'`**
  > Validate *value* against this field's constraints.

### Class `ForeignKeyField`

Integer foreign key referencing another Model's primary key.

Accepts either an integer PK or a model instance::

    class Post(Model):
        author = ForeignKeyField(to=User)

    post.author = user_instance  # stores PK, caches instance
    post.author = 3              # stores raw PK


#### Methods

- **`__init__(self, *, to: 'type[Model] | str', on_delete: 'str' = 'CASCADE', related_name: 'str | None' = None, **kwargs: 'Any')`**

- **`column_ddl(self) -> 'str'`**

- **`to_db(self, value: 'Any') -> 'Any'`**

- **`to_python(self, value: 'Any') -> 'Any'`**
  > Convert *value* coming from the database (or user) to the Python type.

- **`validate(self, value: 'Any') -> 'None'`**

### Class `IPAddressField`

Stored as TEXT, exposed as :class:`~ipaddress.IPv4Address` or
:class:`~ipaddress.IPv6Address`::

    class AccessLog(Model):
        client_ip = IPAddressField()


#### Methods

- **`__init__(self, *, primary_key: 'bool' = False, nullable: 'bool' = False, default: 'Any' = <object object at 0x0000020275050B10>, db_default: 'str | None' = None, unique: 'bool' = False, index: 'bool' = False, column_name: 'str' = '', validators: 'Sequence[Callable[[Any], None]] | None' = None, check: 'str | None' = None)`**

- **`column_ddl(self) -> 'str'`**
  > Return the full column-definition fragment for CREATE TABLE.

- **`to_db(self, value: 'Any') -> 'str | None'`**

- **`to_python(self, value: 'Any') -> 'ipaddress.IPv4Address | ipaddress.IPv6Address'`**

- **`validate(self, value: 'Any') -> 'None'`**

### Class `IntegerField`

#### Methods

- **`__init__(self, *, primary_key: 'bool' = False, nullable: 'bool' = False, default: 'Any' = <object object at 0x0000020275050B10>, db_default: 'str | None' = None, unique: 'bool' = False, index: 'bool' = False, column_name: 'str' = '', validators: 'Sequence[Callable[[Any], None]] | None' = None, check: 'str | None' = None)`**

- **`column_ddl(self) -> 'str'`**
  > Return the full column-definition fragment for CREATE TABLE.

- **`to_db(self, value: 'Any') -> 'Any'`**
  > Convert a Python value to something SQLite can store.

- **`to_python(self, value: 'Any') -> 'Any'`**
  > Convert *value* coming from the database (or user) to the Python type.

- **`validate(self, value: 'Any') -> 'None'`**
  > Validate *value* against this field's constraints.

### Class `JSONField`

Stored as TEXT (JSON) in SQLite, exposed as Python dict/list/scalar.

Supports any JSON-serializable Python value::

    class Config(Model):
        data = JSONField(default=dict)

    config = Config.create(data={"theme": "dark", "lang": "en"})


#### Methods

- **`__init__(self, *, primary_key: 'bool' = False, nullable: 'bool' = False, default: 'Any' = <object object at 0x0000020275050B10>, db_default: 'str | None' = None, unique: 'bool' = False, index: 'bool' = False, column_name: 'str' = '', validators: 'Sequence[Callable[[Any], None]] | None' = None, check: 'str | None' = None)`**

- **`column_ddl(self) -> 'str'`**
  > Return the full column-definition fragment for CREATE TABLE.

- **`to_db(self, value: 'Any') -> 'str | None'`**

- **`to_python(self, value: 'Any') -> 'Any'`**

- **`validate(self, value: 'Any') -> 'None'`**

### Class `PickleField`

Stored as BLOB (pickled bytes), exposed as arbitrary Python objects.

Use for complex Python objects that don't have a natural SQL mapping::

    class Task(Model):
        metadata = PickleField(nullable=True)


#### Methods

- **`__init__(self, *, primary_key: 'bool' = False, nullable: 'bool' = False, default: 'Any' = <object object at 0x0000020275050B10>, db_default: 'str | None' = None, unique: 'bool' = False, index: 'bool' = False, column_name: 'str' = '', validators: 'Sequence[Callable[[Any], None]] | None' = None, check: 'str | None' = None)`**

- **`column_ddl(self) -> 'str'`**
  > Return the full column-definition fragment for CREATE TABLE.

- **`to_db(self, value: 'Any') -> 'bytes | None'`**

- **`to_python(self, value: 'Any') -> 'Any'`**

- **`validate(self, value: 'Any') -> 'None'`**

### Class `RealField`

#### Methods

- **`__init__(self, *, primary_key: 'bool' = False, nullable: 'bool' = False, default: 'Any' = <object object at 0x0000020275050B10>, db_default: 'str | None' = None, unique: 'bool' = False, index: 'bool' = False, column_name: 'str' = '', validators: 'Sequence[Callable[[Any], None]] | None' = None, check: 'str | None' = None)`**

- **`column_ddl(self) -> 'str'`**
  > Return the full column-definition fragment for CREATE TABLE.

- **`to_db(self, value: 'Any') -> 'Any'`**
  > Convert a Python value to something SQLite can store.

- **`to_python(self, value: 'Any') -> 'float'`**

- **`validate(self, value: 'Any') -> 'None'`**

### Class `SlugField`

A :class:`TextField` that validates URL-safe slug format.

Slugs are lowercase alphanumeric strings separated by hyphens::

    class Article(Model):
        slug = SlugField(max_length=200, unique=True, index=True)


#### Methods

- **`__init__(self, *, max_length: 'int' = 255, **kwargs: 'Any')`**

- **`column_ddl(self) -> 'str'`**
  > Return the full column-definition fragment for CREATE TABLE.

- **`to_db(self, value: 'Any') -> 'Any'`**
  > Convert a Python value to something SQLite can store.

- **`to_python(self, value: 'Any') -> 'Any'`**
  > Convert *value* coming from the database (or user) to the Python type.

- **`validate(self, value: 'Any') -> 'None'`**

### Class `TextField`

#### Methods

- **`__init__(self, *, max_length: 'int | None' = None, **kwargs: 'Any')`**

- **`column_ddl(self) -> 'str'`**
  > Return the full column-definition fragment for CREATE TABLE.

- **`to_db(self, value: 'Any') -> 'Any'`**
  > Convert a Python value to something SQLite can store.

- **`to_python(self, value: 'Any') -> 'Any'`**
  > Convert *value* coming from the database (or user) to the Python type.

- **`validate(self, value: 'Any') -> 'None'`**

### Class `TimeField`

Stored as ISO-8601 TEXT (time only) in SQLite, exposed as ``datetime.time``.

#### Methods

- **`__init__(self, *, primary_key: 'bool' = False, nullable: 'bool' = False, default: 'Any' = <object object at 0x0000020275050B10>, db_default: 'str | None' = None, unique: 'bool' = False, index: 'bool' = False, column_name: 'str' = '', validators: 'Sequence[Callable[[Any], None]] | None' = None, check: 'str | None' = None)`**

- **`column_ddl(self) -> 'str'`**
  > Return the full column-definition fragment for CREATE TABLE.

- **`to_db(self, value: 'Any') -> 'str | None'`**

- **`to_python(self, value: 'Any') -> 'datetime.time'`**

- **`validate(self, value: 'Any') -> 'None'`**
  > Validate *value* against this field's constraints.

### Class `TimestampField`

Stored as INTEGER (Unix epoch seconds) in SQLite, exposed as ``datetime.datetime``.

Faster for filtering and sorting than ISO-8601 text, and uses less storage.


#### Methods

- **`__init__(self, *, primary_key: 'bool' = False, nullable: 'bool' = False, default: 'Any' = <object object at 0x0000020275050B10>, db_default: 'str | None' = None, unique: 'bool' = False, index: 'bool' = False, column_name: 'str' = '', validators: 'Sequence[Callable[[Any], None]] | None' = None, check: 'str | None' = None)`**

- **`column_ddl(self) -> 'str'`**
  > Return the full column-definition fragment for CREATE TABLE.

- **`to_db(self, value: 'Any') -> 'int | None'`**

- **`to_python(self, value: 'Any') -> 'datetime.datetime'`**

- **`validate(self, value: 'Any') -> 'None'`**

### Class `UUIDField`

Stored as TEXT, exposed as :class:`uuid.UUID`.

#### Methods

- **`__init__(self, *, primary_key: 'bool' = False, nullable: 'bool' = False, default: 'Any' = <object object at 0x0000020275050B10>, db_default: 'str | None' = None, unique: 'bool' = False, index: 'bool' = False, column_name: 'str' = '', validators: 'Sequence[Callable[[Any], None]] | None' = None, check: 'str | None' = None)`**

- **`column_ddl(self) -> 'str'`**
  > Return the full column-definition fragment for CREATE TABLE.

- **`to_db(self, value: 'Any') -> 'str | None'`**

- **`to_python(self, value: 'Any') -> 'uuid.UUID'`**

- **`validate(self, value: 'Any') -> 'None'`**

## Module: `obele.orm.query`

Fluent query builder for the ORM with sync and async APIs.

``QuerySet`` instances are returned from ``Model.filter()`` and friends.
They are lazily evaluated - SQL is only executed when results are
materialized (via ``all()``, ``first()``, iteration, etc.).

Async counterparts are prefixed with ``a`` (for example ``aall`` and
``afirst``).


### Class `Avg`

#### Methods

- **`__init__(self, *args: 'Any') -> 'None'`**

- **`as_sql(self, queryset: 'QuerySet') -> 'tuple[str, list[Any]]'`**

### Class `Count`

#### Methods

- **`__init__(self, *args: 'Any') -> 'None'`**

- **`as_sql(self, queryset: 'QuerySet') -> 'tuple[str, list[Any]]'`**

### Class `Expression`

Base class for SQL expressions.

#### Methods

- **`as_sql(self, queryset: 'QuerySet') -> 'tuple[str, list[Any]]'`**

### Class `F`

Reference a model field (or joined field) by dotted path.

#### Methods

- **`__init__(self, field_path: 'str') -> 'None'`**

- **`as_sql(self, queryset: 'QuerySet') -> 'tuple[str, list[Any]]'`**

### Class `Func`

Call an SQL function with arguments.

#### Methods

- **`__init__(self, name: 'str', *args: 'Any', is_aggregate: 'bool' = False) -> 'None'`**

- **`as_sql(self, queryset: 'QuerySet') -> 'tuple[str, list[Any]]'`**

### Class `Max`

#### Methods

- **`__init__(self, *args: 'Any') -> 'None'`**

- **`as_sql(self, queryset: 'QuerySet') -> 'tuple[str, list[Any]]'`**

### Class `Min`

#### Methods

- **`__init__(self, *args: 'Any') -> 'None'`**

- **`as_sql(self, queryset: 'QuerySet') -> 'tuple[str, list[Any]]'`**

### Class `Q`

Composable boolean query expression.

Supports ``&`` (AND), ``|`` (OR), and ``~`` (NOT)::

    Q(name="Alice") | Q(name="Bob")
    ~Q(age__lt=18)


#### Methods

- **`__init__(self, *children: 'Q', **lookups: 'Any') -> 'None'`**

### Class `QuerySet`

Lazy, chainable SQL query builder with sync and async APIs.

Every mutating method returns a **copy** so the original can be reused.
Async methods are prefixed with ``a`` (e.g. ``aall``, ``afirst``).


#### Methods

- **`__init__(self, model_cls: 'type[Model]') -> 'None'`**

- **`aaggregate(self, func: 'str', field: 'str') -> 'Any'`**

- **`aall(self) -> 'list'`**

- **`acount(self) -> 'int'`**

- **`acursor_paginate(self, *, per_page: 'int' = 20, cursor_field: 'str' = '', after: 'Any' = None, before: 'Any' = None) -> 'Any'`**
  > Async version of :meth:`cursor_paginate`.

- **`adelete(self) -> 'int'`**

- **`aexists(self) -> 'bool'`**

- **`afirst(self) -> 'Any'`**

- **`aget(self, **kwargs: 'Any') -> 'Model'`**

- **`aggregate(self, func: 'str', field: 'str') -> 'Any'`**
  > Run an aggregate function (SUM, AVG, MIN, MAX, COUNT).

- **`aiterator(self, chunk_size: 'int' = 2000) -> 'AsyncIterator'`**
  > Async streaming results.

- **`all(self) -> 'list'`**
  > Execute the query and return results.

- **`annotate(self, **annotations: 'Any') -> 'QuerySet'`**
  > Add computed columns via expressions::

- **`apaginate(self, *, page: 'int' = 1, per_page: 'int' = 20) -> 'Any'`**
  > Async version of :meth:`paginate`.

- **`as_sql(self) -> 'tuple[str, list[Any]]'`**
  > Return the ``(sql, params)`` tuple without executing.

- **`aupdate(self, **kwargs: 'Any') -> 'int'`**

- **`count(self) -> 'int'`**
  > Return the count of matching rows.

- **`cursor_paginate(self, *, per_page: 'int' = 20, cursor_field: 'str' = '', after: 'Any' = None, before: 'Any' = None) -> 'Any'`**
  > Return a cursor-based :class:`~obele.orm.pagination.CursorPage`.

- **`defer(self, *fields: 'str') -> 'QuerySet'`**
  > Defer loading of specified fields.

- **`delete(self) -> 'int'`**
  > Bulk DELETE matching rows.  Returns number of rows affected.

- **`difference(self, other: 'QuerySet') -> 'QuerySet'`**
  > Combine with another QuerySet using EXCEPT.

- **`distinct(self) -> 'QuerySet'`**
  > Add ``SELECT DISTINCT``.

- **`exclude(self, *conditions: 'Q', **kwargs: 'Any') -> 'QuerySet'`**
  > Add negated ``WHERE`` conditions.

- **`exists(self) -> 'bool'`**
  > Return ``True`` if at least one row matches.

- **`explain(self) -> 'str'`**
  > Return the ``EXPLAIN QUERY PLAN`` output for debugging.

- **`filter(self, *conditions: 'Q', **kwargs: 'Any') -> 'QuerySet'`**
  > Add ``WHERE`` conditions (AND logic).

- **`first(self) -> 'Any'`**
  > Return the first result or ``None``.

- **`get(self, **kwargs: 'Any') -> 'Model'`**
  > Return exactly one result.  Raises on 0 or >1 matches.

- **`group_by(self, *fields: 'str') -> 'QuerySet'`**
  > Add explicit GROUP BY columns.

- **`having(self, *conditions: 'Q', **kwargs: 'Any') -> 'QuerySet'`**
  > Add HAVING conditions for filtered aggregates.

- **`intersection(self, other: 'QuerySet') -> 'QuerySet'`**
  > Combine with another QuerySet using INTERSECT.

- **`iterator(self, chunk_size: 'int' = 2000) -> 'Iterator'`**
  > Stream results row-by-row without materializing the full list.

- **`join(self, relation_name: 'str', *, join_type: 'str' = 'INNER') -> 'QuerySet'`**
  > Explicitly join on a relation (forward FK or reverse).

- **`limit(self, n: 'int') -> 'QuerySet'`**

- **`offset(self, n: 'int') -> 'QuerySet'`**

- **`only(self, *fields: 'str') -> 'QuerySet'`**
  > Load only the specified fields (plus the PK).

- **`order_by(self, *fields: 'str') -> 'QuerySet'`**

- **`paginate(self, *, page: 'int' = 1, per_page: 'int' = 20) -> 'Any'`**
  > Return an offset-based :class:`~obele.orm.pagination.Page`.

- **`prefetch_related(self, *relations: 'str') -> 'QuerySet'`**
  > Batch-load reverse FK relations in separate queries.

- **`select_related(self, *fk_fields: 'str') -> 'QuerySet'`**
  > Eagerly join on ForeignKeyField columns and hydrate related objects.

- **`union(self, other: 'QuerySet', *, all: 'bool' = False) -> 'QuerySet'`**
  > Combine with another QuerySet using UNION.

- **`update(self, *, validate: 'bool' = True, **kwargs: 'Any') -> 'int'`**
  > Bulk UPDATE matching rows.  Returns number of rows affected.

- **`values(self, *fields: 'str') -> 'QuerySet'`**
  > Return dicts of specified fields instead of model instances.

- **`values_list(self, *fields: 'str', flat: 'bool' = False) -> 'QuerySet'`**
  > Return tuples (or flat list if ``flat=True``) of specified fields.

### Class `RawSQL`

Inject raw SQL with optional parameter bindings.

#### Methods

- **`__init__(self, sql: 'str', params: 'list[Any] | tuple[Any, ...] | None' = None) -> 'None'`**

- **`as_sql(self, queryset: 'QuerySet') -> 'tuple[str, list[Any]]'`**

### Class `Subquery`

Embed another QuerySet as a subquery.

#### Methods

- **`__init__(self, queryset: 'QuerySet', field: 'str | None' = None) -> 'None'`**

- **`as_sql(self, queryset: 'QuerySet') -> 'tuple[str, list[Any]]'`**

### Class `Sum`

#### Methods

- **`__init__(self, *args: 'Any') -> 'None'`**

- **`as_sql(self, queryset: 'QuerySet') -> 'tuple[str, list[Any]]'`**

### Class `Value`

Wrap a literal Python value as an SQL parameter.

#### Methods

- **`__init__(self, value: 'Any') -> 'None'`**

- **`as_sql(self, queryset: 'QuerySet') -> 'tuple[str, list[Any]]'`**

### Class `_JoinSpec`

_JoinSpec(path: 'tuple[str, ...]', alias: 'str', related_model: 'type[Model]', relation_name: 'str', relation_kind: 'str', relation_field_name: 'str', sql: 'str')

#### Methods

- **`__init__(self, path: 'tuple[str, ...]', alias: 'str', related_model: 'type[Model]', relation_name: 'str', relation_kind: 'str', relation_field_name: 'str', sql: 'str') -> None`**

## Module: `obele.orm.mixins`

Reusable model mixins for common patterns.

Provides :class:`TimestampMixin` and :class:`SoftDeleteMixin` that can be
composed with :class:`~obele.orm.model.Model` via multiple inheritance::

    from obele import Model, TimestampMixin, SoftDeleteMixin

    class Article(TimestampMixin, SoftDeleteMixin, Model):
        title = TextField()

    article = Article.create(title="Hello")
    article.created_at   # auto-set on insert
    article.updated_at   # auto-set on every save

    article.delete()     # soft-deletes (sets is_deleted=True, deleted_at=now)
    Article.all()        # excludes soft-deleted rows
    Article.with_deleted().all()   # includes soft-deleted
    Article.only_deleted().all()   # only soft-deleted

    article.restore()    # un-deletes
    article.hard_delete()  # permanent removal


### Class `SoftDeleteMixin`

Adds soft-delete support via ``is_deleted`` and ``deleted_at`` fields.

Calling :meth:`delete` sets ``is_deleted=True`` and ``deleted_at=now``
instead of removing the row.  Use :meth:`hard_delete` for permanent
removal.  Default queries exclude soft-deleted rows.


#### Methods

- **`adelete(self) -> 'None'`**
  > Async version of :meth:`delete`.

- **`ahard_delete(self) -> 'None'`**
  > Async version of :meth:`hard_delete`.

- **`arestore(self) -> 'None'`**
  > Async version of :meth:`restore`.

- **`delete(self) -> 'None'`**
  > Soft-delete this instance (mark as deleted, keep the row).

- **`hard_delete(self) -> 'None'`**
  > Permanently remove this instance from the database.

- **`restore(self) -> 'None'`**
  > Un-delete a soft-deleted instance.

### Class `TimestampMixin`

Adds ``created_at`` and ``updated_at`` auto-managed fields.

``created_at`` is set once on first save.  ``updated_at`` is refreshed
on every save.


#### Methods

- **`asave(self) -> 'None'`**
  > Async version of :meth:`save`.

- **`save(self) -> 'None'`**
  > Override save to auto-set timestamps.

## Module: `obele.orm.pagination`

Pagination utilities for QuerySet results.

Provides :class:`Page` and :class:`CursorPage` for offset-based and
cursor-based pagination::

    # Offset-based
    page = User.order_by("name").paginate(page=2, per_page=25)
    page.items       # list[Model]
    page.total       # total matching rows
    page.pages       # total number of pages
    page.has_next    # bool
    page.has_prev    # bool

    # Cursor-based (efficient for large datasets)
    page = User.order_by("id").cursor_paginate(per_page=25)
    next_page = User.order_by("id").cursor_paginate(per_page=25, after=page.end_cursor)


### Class `CursorPage`

Result of cursor-based pagination.

Attributes
----------
items:
    The rows for the current page.
per_page:
    Maximum items per page.
has_next:
    ``True`` if there are more items after this page.
has_prev:
    ``True`` if there are items before this page.
start_cursor:
    Cursor pointing to the first item (use as ``before``).
end_cursor:
    Cursor pointing to the last item (use as ``after``).


#### Methods

- **`__init__(self, items: 'list[Any]', per_page: 'int', has_next: 'bool', has_prev: 'bool', start_cursor: 'Any', end_cursor: 'Any') -> None`**

### Class `Page`

Result of offset-based pagination.

Attributes
----------
items:
    The rows for the current page.
page:
    Current page number (1-indexed).
per_page:
    Maximum items per page.
total:
    Total number of matching rows across all pages.
pages:
    Total number of pages.
has_next:
    ``True`` if there is a page after this one.
has_prev:
    ``True`` if there is a page before this one.


#### Methods

- **`__init__(self, items: 'list[Any]', page: 'int', per_page: 'int', total: 'int', pages: 'int', has_next: 'bool', has_prev: 'bool') -> None`**

### Functions

- **`acursor_paginate_queryset(queryset: 'Any', *, per_page: 'int' = 20, cursor_field: 'str' = '', after: 'Any' = None, before: 'Any' = None) -> 'CursorPage'`**
  > Async version of :func:`cursor_paginate_queryset`.

- **`apaginate_queryset(queryset: 'Any', *, page: 'int' = 1, per_page: 'int' = 20) -> 'Page'`**
  > Async version of :func:`paginate_queryset`.

- **`cursor_paginate_queryset(queryset: 'Any', *, per_page: 'int' = 20, cursor_field: 'str' = '', after: 'Any' = None, before: 'Any' = None) -> 'CursorPage'`**
  > Execute cursor-based pagination on *queryset*.

- **`paginate_queryset(queryset: 'Any', *, page: 'int' = 1, per_page: 'int' = 20) -> 'Page'`**
  > Execute an offset-based pagination on *queryset*.

## Module: `obele.orm.search`

Full-text search via SQLite FTS5.

Provides :class:`SearchIndex` for managing FTS5 virtual tables tied to
ORM models::

    from obele import Database, Model, TextField, SearchIndex

    Database.configure("app.sqlite3")

    class Article(Model):
        title = TextField()
        body  = TextField()

    Article.create_table()

    # Create an FTS5 index over title and body
    idx = SearchIndex(Article, fields=["title", "body"])
    idx.create()

    Article.create(title="Python Async", body="asyncio is great")
    Article.create(title="SQLite Tips", body="WAL mode is fast")

    idx.rebuild()  # Sync FTS with current table data

    results = idx.search("async")       # Ranked list of Article instances
    results = idx.search("sqlite tips") # FTS5 match syntax supported


### Class `SearchIndex`

FTS5 full-text search index tied to a :class:`Model`.

Parameters
----------
model_cls:
    The model class to index.
fields:
    List of ``TextField`` attribute names to include in the index.
fts_table:
    Custom FTS virtual table name.  Defaults to
    ``{model.table_name}_fts``.
tokenizer:
    FTS5 tokenizer specification (e.g. ``"porter unicode61"``).
content_sync:
    If ``True`` (default), creates a *content* FTS table that
    mirrors the source table. Set to ``False`` for an external
    content table that you manage manually.


#### Methods

- **`__init__(self, model_cls: 'type[Model]', fields: 'list[str]', *, fts_table: 'str | None' = None, tokenizer: 'str' = 'unicode61', content_sync: 'bool' = True) -> 'None'`**

- **`acreate(self) -> 'None'`**
  > Async version of :meth:`create`.

- **`adrop(self) -> 'None'`**
  > Async version of :meth:`drop`.

- **`aoptimize(self) -> 'None'`**
  > Async version of :meth:`optimize`.

- **`arebuild(self) -> 'None'`**
  > Async version of :meth:`rebuild`.

- **`asearch(self, query: 'str', *, limit: 'int | None' = None, offset: 'int | None' = None) -> 'list[Model]'`**
  > Async version of :meth:`search`.

- **`asearch_count(self, query: 'str') -> 'int'`**
  > Async version of :meth:`search_count`.

- **`create(self) -> 'None'`**
  > Create the FTS5 virtual table.

- **`drop(self) -> 'None'`**
  > Drop the FTS5 virtual table and associated triggers.

- **`optimize(self) -> 'None'`**
  > Run FTS5 merge optimization.

- **`rebuild(self) -> 'None'`**
  > Rebuild the FTS index from the source table data.

- **`search(self, query: 'str', *, limit: 'int | None' = None, offset: 'int | None' = None) -> 'list[Model]'`**
  > Search the FTS index and return ranked model instances.

- **`search_count(self, query: 'str') -> 'int'`**
  > Return the number of rows matching the FTS query.

## Module: `obele.orm.signals`

Signal/hook system for model lifecycle events.

Provides a lightweight publish-subscribe mechanism for decoupled
model lifecycle logic::

    from obele import pre_save, post_save, receiver

    @receiver(pre_save, sender=User)
    def hash_password(sender, instance, **kwargs):
        if 'password' in instance.dirty_fields:
            instance.password = bcrypt.hash(instance.password)

    @receiver(post_save)
    def log_save(sender, instance, created, **kwargs):
        print(f"{'Created' if created else 'Updated'} {sender.__name__} pk={instance.pk}")


### Class `Signal`

A signal that receivers can connect to and that senders can emit.

Thread-safe and supports optional sender filtering.

Parameters
----------
name:
    Human-readable signal name (used in ``repr``).
providing_args:
    Documentation-only list of keyword argument names that
    :meth:`send` will provide to receivers.


#### Methods

- **`__init__(self, name: 'str' = '', providing_args: 'list[str] | None' = None) -> 'None'`**

- **`connect(self, receiver: '_Receiver', *, sender: 'type[Model] | None' = None, weak: 'bool' = False) -> 'None'`**
  > Register *receiver* to be called when this signal is sent.

- **`disconnect(self, receiver: '_Receiver', *, sender: 'type[Model] | None' = None) -> 'bool'`**
  > Remove a previously connected receiver.  Returns ``True`` if found.

- **`has_receivers(self, sender: 'type[Model] | None' = None) -> 'bool'`**
  > Return ``True`` if any receivers are connected.

- **`send(self, sender: 'type[Model]', **kwargs: 'Any') -> 'list[tuple[_Receiver, Any]]'`**
  > Emit this signal.  Returns ``[(receiver, return_value), ...]``.

### Functions

- **`receiver(signal: 'Signal | list[Signal]', *, sender: 'type[Model] | None' = None) -> 'Callable[[_Receiver], _Receiver]'`**
  > Decorator to connect a function to one or more signals::

## Module: `obele.orm.exceptions`

Custom exception hierarchy for the ORM.

### Class `ConfigurationError`

Raised when Database.configure() is called with invalid arguments.

### Class `DatabaseError`

Wraps sqlite3.Error for ORM-level handling.

### Class `FieldValidationError`

Raised when a field value fails validation (type mismatch, constraint violation).

### Class `IntegrityError`

Wraps sqlite3.IntegrityError for constraint violations.

### Class `MigrationError`

Raised when a schema migration fails.

### Class `MultipleResultsError`

Raised when a query expected exactly one result but found multiple.

### Class `ORMError`

Base exception for all ORM errors.

### Class `RecordNotFoundError`

Raised when a query expected exactly one result but found none.

## Module: `obele.kv.store`

Single-table key-value storage built on top of :mod:`obele.orm.database`.

Provides :class:`KVStore`, a :class:`~collections.abc.MutableMapping` with
support for sorted iteration, slicing, range queries, multi-key lookups,
batch writes, TTL expiration, and pluggable serialization.

Usage::

    from obele import Database, KVStore

    Database.configure("myapp.sqlite3")
    store = KVStore("settings")

    store["theme"] = "dark"
    store["lang"]  = "en"
    print(store["theme"])             # "dark"
    print(store["a":"z"])             # dict of all keys in [a, z)

    # TTL support
    store.set("temp", "value", ttl=300)  # expires in 5 minutes

    # Multi-key
    store.get_many("theme", "lang")   # {"theme": "dark", "lang": "en"}


### Class `KVStore`

A fast, single-table key-value store with a dict-like interface.

Parameters
----------
table_name:
    SQLite table name (must be a valid identifier).
key_type:
    Optional Python type constraining keys - ``int``, ``float``,
    ``str``, or ``bytes``.
enforce_key_type:
    When ``True`` (default), all keys must share the same sortable
    type, enabling ordered iteration and range queries.
serializer:
    ``"auto"`` (default) tries JSON first then falls back to pickle.
    ``"json"`` forces JSON-only.  ``"pickle"`` forces pickle-only.
    A ``(dumps, loads)`` callable pair for custom serialization.
namespace:
    Optional string prefix applied to all keys, enabling multiple
    logical stores in one table.


#### Methods

- **`__init__(self, table_name: 'str' = 'kv_store', *, key_type: 'type[Any] | None' = None, enforce_key_type: 'bool' = True, serializer: 'SerializerMode | tuple[_Dumps, _Loads]' = 'auto', namespace: 'str | None' = None) -> 'None'`**

- **`aclear(self) -> 'None'`**
  > Async version of :meth:`clear`.

- **`acompare_and_swap(self, key: 'Any', expected: 'Any', new_value: 'Any', *, ttl: 'float | int | None' = None) -> 'bool'`**
  > Async version of :meth:`compare_and_swap`.

- **`acontains(self, key: 'Any') -> 'bool'`**
  > Async version of :meth:`__contains__`.

- **`acreate_table(self, if_not_exists: 'bool' = True) -> 'None'`**
  > Async version of :meth:`create_table`.

- **`adelete(self, key: 'Any') -> 'None'`**
  > Async ``__delitem__``.

- **`adelete_many(self, keys: 'Sequence[Any] | Iterable[Any]') -> 'int'`**
  > Async version of :meth:`delete_many`.

- **`adrop_table(self, if_exists: 'bool' = True) -> 'None'`**
  > Async version of :meth:`drop_table`.

- **`aexpire(self, key: 'Any', ttl: 'float | int') -> 'bool'`**
  > Async version of :meth:`expire`.

- **`aget(self, key: 'Any', default: 'Any' = None) -> 'Any'`**
  > Async version of :meth:`get`.

- **`aget_many(self, *keys: 'Any', **kwargs: 'Any') -> 'dict[Any, Any] | tuple[tuple[Any, Any], ...]'`**
  > Async version of :meth:`get_many`.

- **`aincrement(self, key: 'Any', delta: 'int | float' = 1) -> 'int | float'`**
  > Async version of :meth:`increment`.

- **`aitems(self) -> 'list[tuple[Any, Any]]'`**
  > Async version of :meth:`items`.

- **`akeys(self) -> 'list[Any]'`**
  > Async version of :meth:`keys`.

- **`akeys_slice(self, start: 'Any' = None, stop: 'Any' = None, *, step: 'int | None' = None, reverse: 'bool' = False) -> 'tuple[Any, ...]'`**
  > Async version of :meth:`keys_slice`.

- **`alen(self) -> 'int'`**
  > Async version of :meth:`__len__`.

- **`apersist(self, key: 'Any') -> 'bool'`**
  > Async version of :meth:`persist`.

- **`apop(self, key: 'Any', *args: 'Any') -> 'Any'`**
  > Async version of :meth:`pop`.

- **`apopitem(self, last: 'bool' = True) -> 'tuple[Any, Any]'`**
  > Async version of :meth:`popitem`.

- **`aprefix(self, prefix: 'str', *, limit: 'int | None' = None, reverse: 'bool' = False, return_type: 'MultiGetReturn' = 'dict') -> 'dict[Any, Any] | tuple[tuple[Any, Any], ...]'`**
  > Async version of :meth:`prefix`.

- **`aprefix_count(self, prefix: 'str') -> 'int'`**
  > Async version of :meth:`prefix_count`.

- **`aprefix_delete(self, prefix: 'str') -> 'int'`**
  > Async version of :meth:`prefix_delete`.

- **`aprefix_keys(self, prefix: 'str', *, limit: 'int | None' = None) -> 'list[str]'`**
  > Async version of :meth:`prefix_keys`.

- **`apurge_expired(self) -> 'int'`**
  > Async version of :meth:`purge_expired`.

- **`arange(self, start: 'Any' = None, stop: 'Any' = None, *, step: 'int | None' = None, reverse: 'bool' = False, return_type: 'MultiGetReturn' = 'dict') -> 'dict[Any, Any] | tuple[tuple[Any, Any], ...]'`**
  > Async version of :meth:`range`.

- **`ascan(self, pattern: 'str' = '*', *, limit: 'int | None' = None, return_type: 'MultiGetReturn' = 'dict') -> 'dict[Any, Any] | tuple[tuple[Any, Any], ...]'`**
  > Async version of :meth:`scan`.

- **`aset(self, key: 'Any', value: 'Any', *, serializer: 'SerializerMode | None' = None, ttl: 'float | int | None' = None) -> 'None'`**
  > Async ``__setitem__`` with optional TTL.

- **`aset_many(self, items: 'Mapping[Any, Any] | Iterable[tuple[Any, Any]]', *, serializer: 'SerializerMode | None' = None, ttl: 'float | int | None' = None) -> 'None'`**
  > Async version of :meth:`set_many`.

- **`asetdefault(self, key: 'Any', default: 'Any' = None) -> 'Any'`**
  > Async version of :meth:`setdefault`.

- **`astats(self) -> 'dict[str, Any]'`**
  > Async version of :meth:`stats`.

- **`attl(self, key: 'Any') -> 'float | None'`**
  > Async version of :meth:`ttl`.

- **`aupdate(self, other: 'Mapping[Any, Any] | Iterable[tuple[Any, Any]]' = (), /, **kwargs: 'Any') -> 'None'`**
  > Async version of :meth:`update`.

- **`avalues(self) -> 'list[Any]'`**
  > Async version of :meth:`values`.

- **`avalues_slice(self, start: 'Any' = None, stop: 'Any' = None, *, step: 'int | None' = None, reverse: 'bool' = False) -> 'tuple[Any, ...]'`**
  > Async version of :meth:`values_slice`.

- **`clear(self) -> 'None'`**
  > Remove **all** key-value pairs from the store.

- **`compare_and_swap(self, key: 'Any', expected: 'Any', new_value: 'Any', *, ttl: 'float | int | None' = None) -> 'bool'`**
  > Atomically set ``key`` to ``new_value`` if its value equals ``expected``.

- **`create_table(self, if_not_exists: 'bool' = True) -> 'None'`**
  > Explicitly (re-)create the backing table.

- **`delete(self, key: 'Any') -> 'None'`**
  > Delete a single key. Raises ``KeyError`` if not present.

- **`delete_many(self, keys: 'Sequence[Any] | Iterable[Any]') -> 'int'`**
  > Delete multiple keys at once.  Returns the number of rows removed.

- **`drop_table(self, if_exists: 'bool' = True) -> 'None'`**
  > Drop the backing table.

- **`expire(self, key: 'Any', ttl: 'float | int') -> 'bool'`**
  > Set a new TTL for an existing key.

- **`get(self, key: 'Any', default: 'Any' = None) -> 'Any'`**
  > Return the value for *key*, or *default* if not present.

- **`get_many(self, *keys: 'Any', return_type: 'MultiGetReturn' = 'dict', default: 'Any' = <object object at 0x0000020275050BB0>, skip_missing: 'bool' = False) -> 'dict[Any, Any] | tuple[tuple[Any, Any], ...]'`**
  > Fetch many keys at once.

- **`increment(self, key: 'Any', delta: 'int | float' = 1) -> 'int | float'`**
  > Atomically increment a numeric value. Creates the key if missing.

- **`items(self) -> 'list[tuple[Any, Any]]'`**
  > Return all ``(key, value)`` pairs, ordered by key.

- **`keys(self) -> 'list[Any]'`**
  > Return all keys, in sort order.

- **`keys_slice(self, start: 'Any' = None, stop: 'Any' = None, *, step: 'int | None' = None, reverse: 'bool' = False) -> 'tuple[Any, ...]'`**
  > Return only keys from a :meth:`range` query.

- **`persist(self, key: 'Any') -> 'bool'`**
  > Remove the TTL from an existing key.

- **`pop(self, key: 'Any', *args: 'Any') -> 'Any'`**
  > Remove and return the value for *key*.

- **`popitem(self, last: 'bool' = True) -> 'tuple[Any, Any]'`**
  > Remove and return an arbitrary ``(key, value)`` pair.

- **`prefix(self, prefix: 'str', *, limit: 'int | None' = None, reverse: 'bool' = False, return_type: 'MultiGetReturn' = 'dict') -> 'dict[Any, Any] | tuple[tuple[Any, Any], ...]'`**
  > Return all entries whose string key starts with *prefix*.

- **`prefix_count(self, prefix: 'str') -> 'int'`**
  > Return count of keys matching *prefix*.

- **`prefix_delete(self, prefix: 'str') -> 'int'`**
  > Delete all keys matching *prefix*. Returns number of rows removed.

- **`prefix_keys(self, prefix: 'str', *, limit: 'int | None' = None) -> 'list[str]'`**
  > Return only the keys matching *prefix* (no values loaded).

- **`purge_expired(self) -> 'int'`**
  > Delete all expired entries. Returns number of rows removed.

- **`range(self, start: 'Any' = None, stop: 'Any' = None, *, step: 'int | None' = None, reverse: 'bool' = False, return_type: 'MultiGetReturn' = 'dict') -> 'dict[Any, Any] | tuple[tuple[Any, Any], ...]'`**
  > Return items whose keys lie in ``[start, stop)``.

- **`scan(self, pattern: 'str' = '*', *, limit: 'int | None' = None, return_type: 'MultiGetReturn' = 'dict') -> 'dict[Any, Any] | tuple[tuple[Any, Any], ...]'`**
  > Return entries whose string key matches a SQL GLOB *pattern*.

- **`set(self, key: 'Any', value: 'Any', *, serializer: 'SerializerMode | None' = None, ttl: 'float | int | None' = None) -> 'None'`**
  > Insert or replace a single key-value pair.

- **`set_many(self, items: 'Mapping[Any, Any] | Iterable[tuple[Any, Any]]', *, serializer: 'SerializerMode | None' = None, ttl: 'float | int | None' = None) -> 'None'`**
  > Insert or replace many key-value pairs efficiently.

- **`setdefault(self, key: 'Any', default: 'Any' = None) -> 'Any'`**
  > Return the value for *key*, inserting *default* first if missing.

- **`stats(self) -> 'dict[str, Any]'`**
  > Return store statistics for monitoring and debugging.

- **`ttl(self, key: 'Any') -> 'float | None'`**
  > Return seconds until expiration, or ``None`` for persistent keys.

- **`update(self, other: 'Mapping[Any, Any] | Iterable[tuple[Any, Any]]' = (), /, **kwargs: 'Any') -> 'None'`**
  > Bulk-insert / update from a mapping, iterable, and/or keyword arguments.

- **`values(self) -> 'list[Any]'`**
  > Return all values, ordered by key.

- **`values_slice(self, start: 'Any' = None, stop: 'Any' = None, *, step: 'int | None' = None, reverse: 'bool' = False) -> 'tuple[Any, ...]'`**
  > Return only values from a :meth:`range` query.

### Class `_EncodedKey`

_EncodedKey(lookup_key: 'bytes', key_format: 'str', key_payload: 'bytes', key_int: 'int | None' = None, key_real: 'float | None' = None, key_text: 'str | None' = None, key_blob: 'bytes | None' = None)

#### Methods

- **`__init__(self, lookup_key: 'bytes', key_format: 'str', key_payload: 'bytes', key_int: 'int | None' = None, key_real: 'float | None' = None, key_text: 'str | None' = None, key_blob: 'bytes | None' = None) -> None`**

### Class `_EncodedValue`

_EncodedValue(value_format: 'str', value_payload: 'bytes')

#### Methods

- **`__init__(self, value_format: 'str', value_payload: 'bytes') -> None`**

