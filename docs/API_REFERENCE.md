# API Reference

This reference covers the public API exposed from `obele`, `obele.orm`, and `obele.kv`.

## Public Imports

Top-level imports:

```python
from obele import (
    Database,
    Model,
    QuerySet,
    Q,
    F,
    Value,
    RawSQL,
    Func,
    Count,
    Sum,
    Avg,
    Min,
    Max,
    Subquery,
    Field,
    IntegerField,
    TextField,
    RealField,
    BlobField,
    BooleanField,
    DateTimeField,
    ForeignKeyField,
    ReverseRelationManager,
    ReverseRelationDescriptor,
    ORMError,
    FieldValidationError,
    RecordNotFoundError,
    MultipleResultsError,
    DatabaseError,
    IntegrityError,
    KVStore,
    KV,
)
```

Equivalent subpackage imports:

```python
from obele.orm import Database, Model, QuerySet
from obele.kv import KVStore, KV
```

## `Database`

`Database` is a class-based SQLite connection manager.

### Core methods

| Method | Purpose |
| --- | --- |
| `configure(db_path="DB.sqlite3", pragmas=None)` | Configure the process-wide default database |
| `aconfigure(...)` | Async wrapper around `configure()` |
| `using(db_path, pragmas=None)` | Create a scoped binding context manager |
| `transaction()` | Open a sync or async transaction context manager |
| `current_config()` | Return `(db_path, pragmas)` for the active binding |
| `get_connection()` | Return the active `sqlite3.Connection` |
| `close()` / `aclose()` | Close the active connection |
| `execute(sql, params=None)` | Execute a write statement and commit |
| `executemany(sql, params_seq)` | Execute many writes and commit |
| `execute_read(sql, params=None)` | Execute a read query and return a cursor-like object |
| `aexecute(...)`, `aexecutemany(...)`, `aexecute_read(...)` | Async wrappers |

### Notes

- file-backed databases enable `PRAGMA journal_mode=WAL`
- `PRAGMA foreign_keys=ON` is always enabled
- reads and writes are serialized on the shared connection
- nested `transaction()` blocks use SQLite savepoints

## `Model`

`Model` is the declarative base class for ORM tables.

### Schema helpers

| Method | Purpose |
| --- | --- |
| `create_table(if_not_exists=True)` | Create the table and configured indexes |
| `acreate_table(...)` | Async wrapper |
| `drop_table(if_exists=True)` | Drop the table |
| `adrop_table(...)` | Async wrapper |
| `migrate(rename_fields=None, create_if_missing=True)` | Schema-sync the table to the declared model |
| `amigrate(...)` | Async wrapper |

### Instance methods

| Method | Purpose |
| --- | --- |
| `save()` / `asave()` | Insert or update the row |
| `delete()` / `adelete()` | Delete the current row |
| `refresh()` / `arefresh()` | Reload the row from SQLite |
| `to_dict(mode="python", include_annotations=True)` | Serialize to a dict |
| `to_db_dict(include_annotations=True)` | Serialize using field `to_db()` conversions |

### Construction helpers

| Method | Purpose |
| --- | --- |
| `create(**kwargs)` / `acreate(**kwargs)` | Construct and save an instance |
| `get_or_create(defaults=None, **kwargs)` / `aget_or_create(...)` | Return `(instance, created)` |
| `bulk_create(items, validate=True)` / `abulk_create(...)` | Insert many rows efficiently |

### Query bridge methods

| Method | Return |
| --- | --- |
| `filter(*conditions, **kwargs)` | `QuerySet` |
| `exclude(*conditions, **kwargs)` | `QuerySet` |
| `order_by(*fields)` | `QuerySet` |
| `limit(n)` | `QuerySet` |
| `offset(n)` | `QuerySet` |
| `select_related(*fk_fields)` | `QuerySet` |
| `join(relation_name, join_type="INNER")` | `QuerySet` |
| `annotate(**annotations)` | `QuerySet` |
| `all()` / `aall()` | `list[Model]` |
| `first()` / `afirst()` | `Model | None` |
| `get(**kwargs)` / `aget(**kwargs)` | `Model` |
| `count()` / `acount()` | `int` |
| `exists()` / `aexists()` | `bool` |
| `aggregate(func, field)` / `aaggregate(func, field)` | `Any` |

### Model behavior

- if no primary key is declared, `id = IntegerField(primary_key=True)` is injected
- `table_name` defaults to the lowercase class name if omitted
- reverse relations are registered automatically for `ForeignKeyField`
- `save()` validates field values before insert or update

## `QuerySet`

`QuerySet` is a lazy, chainable query builder.

### Materialization

SQL runs when you call:

- `all()` / `aall()`
- `first()` / `afirst()`
- `get()` / `aget()`
- `count()` / `acount()`
- `exists()` / `aexists()`
- iteration / async iteration
- `iterator()` / `aiterator()`

### Query methods

| Method | Purpose |
| --- | --- |
| `filter(*conditions, **kwargs)` | Add `WHERE` clauses with `AND` semantics |
| `exclude(*conditions, **kwargs)` | Add negated conditions |
| `order_by(*fields)` | Add `ORDER BY` clauses |
| `limit(n)` | Add `LIMIT` |
| `offset(n)` | Add `OFFSET` |
| `join(relation_name, join_type="INNER")` | Explicitly join a forward or reverse relation |
| `select_related(*fk_fields)` | Add joins and hydrate direct FK relations |
| `annotate(**annotations)` | Add computed select expressions |
| `iterator(chunk_size=2000)` | Stream rows in chunks |
| `aiterator(chunk_size=2000)` | Async chunked iterator |
| `update(validate=True, **kwargs)` | Bulk update |
| `delete()` | Bulk delete |
| `aggregate(func, field)` | Aggregate over the current queryset |

### Supported lookups

| Lookup | Example |
| --- | --- |
| exact | `name="Alice"` |
| `__ne` | `name__ne="Bob"` |
| `__gt`, `__gte`, `__lt`, `__lte` | `age__gte=18` |
| `__like` | `name__like="A%"` |
| `__glob` | `name__glob="A*"` |
| `__in` | `id__in=[1, 2, 3]` |
| `__not_in` | `name__not_in=["Alice", "Bob"]` |
| `__is_null` | `email__is_null=True` |
| `__between`, `__range` | `age__between=(18, 30)` |
| `__contains`, `__startswith`, `__endswith` | `name__contains="ali"` |
| `__iexact`, `__icontains`, `__istartswith`, `__iendswith` | `name__icontains="ALI"` |
| `__regex` | `name__regex="^A.*"` |

Notes:

- relation traversal uses paths such as `author__name="Alice"`
- ordering can use traversed fields such as `order_by("author__name")`
- `__regex` emits `REGEXP`; SQLite needs a `REGEXP` function registered if you intend to use it

### `Q`

`Q` builds grouped boolean expressions:

```python
Q(name="Alice") | Q(age__lt=18)
~Q(active=False)
```

### Expressions

| Class | Purpose |
| --- | --- |
| `F(field_path)` | Reference a field or joined field |
| `Value(value)` | Wrap a literal parameter |
| `RawSQL(sql, params=None)` | Inject raw SQL |
| `Func(name, *args, is_aggregate=False)` | Call an SQL function |
| `Count`, `Sum`, `Avg`, `Min`, `Max` | Aggregate expressions |
| `Subquery(queryset, field=None)` | Embed another queryset |

Annotation aliases may also be used in `order_by(...)`.

## Relations

### `ForeignKeyField`

Constructor options:

| Option | Meaning |
| --- | --- |
| `to` | Related model class or lazy model name |
| `on_delete` | SQLite `ON DELETE` action, default `CASCADE` |
| `related_name` | Custom reverse accessor name |

Behavior:

- accepts either a related model instance or a raw primary key
- caches hydrated related instances on the owning object
- reverse relations are exposed through `ReverseRelationManager`

### `ReverseRelationManager`

Returned by reverse descriptors such as `author.articles` or `post.comment_set`.

Main methods:

- `all()`, `aall()`
- `filter()`, `exclude()`, `order_by()`
- `count()`, `exists()`, `first()`, `get()`
- `create()`, `acreate()`
- iteration and async iteration

## Fields

### Base field options

| Option | Meaning |
| --- | --- |
| `primary_key` | Mark as primary key |
| `nullable` | Allow `NULL` |
| `default` | Python-side default |
| `db_default` | SQLite DDL default expression |
| `unique` | Add `UNIQUE` |
| `index` | Create an index |
| `column_name` | Override the SQL column name |

### Built-in fields

| Field | SQLite type | Python type |
| --- | --- | --- |
| `IntegerField` | `INTEGER` | `int` |
| `TextField` | `TEXT` | `str` |
| `RealField` | `REAL` | `float` |
| `BlobField` | `BLOB` | `bytes` |
| `BooleanField` | `INTEGER` | `bool` |
| `DateTimeField` | `TEXT` | `datetime.datetime` |
| `ForeignKeyField` | `INTEGER` | related model PK or instance |

Special notes:

- `TextField(max_length=...)` enforces length in Python validation
- `DateTimeField` stores ISO 8601 text
- `BooleanField` stores `1` or `0`

## Exceptions

| Exception | Meaning |
| --- | --- |
| `ORMError` | Base exception |
| `FieldValidationError` | Validation or conversion failed |
| `RecordNotFoundError` | A required row was not found |
| `MultipleResultsError` | `get()` found more than one row |
| `DatabaseError` | Translated non-integrity SQLite failure |
| `IntegrityError` | Translated SQLite integrity failure |

## Migration CLI

Run directly:

```bash
python -m obele.orm list-models --module myapp.models
python -m obele.orm migrate --database app.sqlite3 --module myapp.models
```

Installed script:

```bash
obele-orm migrate --database app.sqlite3 --module myapp.models
```

CLI options:

- `--module package.module`
- `--model package.module:ClassName`
- `--database path.sqlite3`
- `--pragma KEY=VALUE`
- `--rename Model.field=old_column`
- `--no-create-if-missing`
- `--quiet`

## `KVStore`

`KVStore` is a `MutableMapping` backed by a single SQLite table.

### Constructor

```python
KVStore(
    table_name="kv_store",
    key_type=None,
    enforce_key_type=True,
    serializer="auto",
)
```

Arguments:

- `table_name`: SQLite identifier for the backing table
- `key_type`: optional sortable key type (`int`, `float`, `str`, or `bytes`)
- `enforce_key_type`: default `True`; keeps keys homogeneous and sortable
- `serializer`: `"auto"`, `"json"`, `"pickle"`, or a `(dumps, loads)` pair

### Dict-like methods

- `store[key]`
- `store[key] = value`
- `del store[key]`
- `key in store`
- `len(store)`
- iteration over keys
- `get()`, `set()`, `delete()`, `pop()`, `popitem()`, `setdefault()`, `update()`, `clear()`

### Batch and slice methods

| Method | Purpose |
| --- | --- |
| `get_many(*keys, return_type="dict", default=..., skip_missing=False)` | Fetch many keys |
| `set_many(items)` | Write many key-value pairs |
| `delete_many(keys)` | Delete many keys |
| `range(start=None, stop=None, step=None, reverse=False, return_type="dict")` | Ordered range query |
| `keys_slice(...)` | Return only keys from a range |
| `values_slice(...)` | Return only values from a range |

Slice syntax:

```python
store[10:20]
```

returns the same kind of result as `range(10, 20)`.

### Async methods

Async wrappers include:

- `acreate_table()`, `adrop_table()`
- `aget()`, `aset()`, `adelete()`
- `apop()`, `apopitem()`, `asetdefault()`
- `aupdate()`, `aclear()`
- `aget_many()`, `aset_many()`, `adelete_many()`
- `akeys()`, `avalues()`, `aitems()`
- `alen()`, `acontains()`

### Serialization behavior

- `"auto"` tries compact JSON first, then falls back to pickle
- JSON mode requires exact type-preserving round-trips
- pickle handles arbitrary Python values but is Python-specific

## `KV`

`KV` is a singleton subclass of `KVStore`.

Behavior:

- the first `KV(...)` call creates the global instance
- later `KV(...)` calls return the same object
- later constructor arguments are ignored
- `KV.reset()` clears the singleton so the next call can reinitialize it

