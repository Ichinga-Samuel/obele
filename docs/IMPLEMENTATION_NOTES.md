# Implementation Notes

This document describes the current design choices, SQLite-specific behavior, and practical caveats in `obele`.

## Verified Coverage

The repository test suite currently covers:

- schema creation and teardown
- schema-sync migrations
- migration CLI behavior
- CRUD flows
- bulk operations and validation controls
- forward and reverse relations
- hydrated `select_related()` access
- `Q` composition, joins, subqueries, and annotations
- sync and async ORM APIs
- key-value CRUD, slicing, range queries, and singleton behavior

## Package Structure

The project is split into two main subpackages:

- `obele.orm`
- `obele.kv`

Top-level `obele` imports re-export the main public classes from those subpackages.

## ORM Architecture

### Shared SQLite Connection

`Database` manages a shared `sqlite3.Connection` for the active binding.

Important characteristics:

- `check_same_thread=False`
- `row_factory = sqlite3.Row`
- WAL mode for file-backed databases
- foreign keys enabled
- a connection lock serializes access to the shared connection

Read queries return a cursor wrapper that keeps the connection lock until rows are consumed or the cursor is closed. That avoids cursor races on the shared connection, but it also means long-running reads block other work on that same binding.

### Scoped Bindings

`Database.configure()` sets the process-wide default binding, but `Database.using()` allows a temporary scoped binding for a block of work:

```python
with Database.using("other.sqlite3"):
    ...
```

This avoids mutating the global default for every caller.

### Transactions

`Database.transaction()` provides multi-statement transaction scopes and uses SQLite savepoints for nested transactions.

## Async Model

The async API is implemented by offloading synchronous `sqlite3` work with `asyncio.to_thread()`.

Practical implications:

- the API fits async applications
- the underlying database driver is still synchronous
- throughput depends on SQLite locking and the thread pool
- it is not equivalent to an async-native driver

## Schema and Migrations

### SQLite-Only Schema Sync

`Model.migrate()` and `Model.amigrate()` implement schema-sync migrations by rebuilding the target table.

Migration strategy:

1. inspect the live table
2. rename the old table
3. create the new schema from the model definition
4. copy compatible columns forward
5. fill new columns from Python defaults when possible
6. keep SQLite `db_default` expressions in DDL
7. recreate indexes
8. drop the old table

Column renames are supported through `rename_fields={new_field: old_column}`.

This is intentionally schema-sync oriented. There is no migration history ledger.

### Migration CLI

The ORM includes a CLI:

```bash
python -m obele.orm list-models --module myapp.models
python -m obele.orm migrate --database app.sqlite3 --module myapp.models
```

or, when installed:

```bash
obele-orm migrate --database app.sqlite3 --module myapp.models
```

The CLI discovers model classes, orders them by foreign-key dependencies when possible, and runs schema-sync migrations against SQLite.

## Query System

### Lookups

The query layer supports:

- basic comparisons
- `IN` and `NOT IN`
- `IS NULL`
- `BETWEEN` / `RANGE`
- string contains / startswith / endswith variants
- case-insensitive text variants
- `GLOB`
- `REGEXP`

`REGEXP` is emitted as SQL only. SQLite does not provide it by default, so consumers must register a compatible function if they want to use that lookup.

### Boolean Expressions

`Q` supports grouped `AND`, `OR`, and `NOT` composition.

### Joins and Traversal

The ORM supports:

- forward relation traversal such as `author__name`
- reverse relation traversal such as `posts__title`
- explicit joins with `join(...)`
- eager loading with `select_related(...)`

Current boundary:

- `select_related()` currently supports direct foreign-key fields only

### Annotations

Annotations are expression-based and SQLite-native. Supported expression helpers include:

- `F`
- `Value`
- `RawSQL`
- `Func`
- `Count`
- `Sum`
- `Avg`
- `Min`
- `Max`
- `Subquery`

Annotation aliases can be used in `order_by(...)`.

This is practical for application queries, but it is not a backend-independent query compiler. That limitation is deliberate, not accidental.

## Relationships

### Forward Foreign Keys

`ForeignKeyField` accepts either:

- a raw related primary key
- a related model instance

When a related instance is assigned or hydrated, the ORM caches it on the owning model.

### Reverse Relations

Reverse relation accessors are registered automatically.

Examples:

- `author.articles`
- `post.comment_set`

These accessors expose a `ReverseRelationManager` with queryset-like helpers and create methods.

### `select_related()` Hydration

`select_related("author")` hydrates the related instance onto the foreign-key field itself:

```python
post = Post.select_related("author").get(id=1)
post.author.name
```

Without `select_related()`, the foreign-key field exposes the raw related primary key.

## Validation and Serialization

### Field Validation

Instance saves validate field values before writing.

Bulk APIs also validate by default:

- `Model.bulk_create(..., validate=True)`
- `QuerySet.update(validate=True, ...)`

Both allow validation bypass for trusted data:

- `validate=False`

### Serialization

`Model.to_dict()` returns Python values by default.

Use:

```python
instance.to_db_dict()
```

when SQLite-serialized output is needed.

## Key-Value Store Notes

### Single-Table Design

`KVStore` stores all data in one SQLite table with:

- a binary `lookup_key` for exact identity
- encoded key payload
- encoded value payload
- dedicated sortable columns for ordered key modes

### Homogeneous Keys by Default

`KVStore` defaults to `enforce_key_type=True`.

That gives:

- one sortable key type per store
- stable ordering
- efficient slicing and range queries

If mixed-type keys are required, `enforce_key_type=False` disables that constraint, but ordered range behavior is no longer well-defined.

### Serialization Strategy

`KVStore` supports:

- JSON
- pickle
- auto mode that prefers JSON and falls back to pickle
- custom `(dumps, loads)` serializers

JSON mode only succeeds when the value round-trips without type drift.

### Singleton `KV`

`KV` is a process-local singleton. It is useful for application-global settings or caches inside one interpreter, but it should not be treated as a distributed coordination primitive.

## Current Deliberate Tradeoffs

The main remaining tradeoffs are:

- SQLite is the only supported backend.
- Async support is thread-offloaded sync work.
- Migrations are schema-sync based and intentionally ledger-free.
- Annotation support is expression-oriented and SQLite-native, not backend-independent.
- `select_related()` does not yet support nested relation paths.
- Long reads on the shared connection can block other work on the same binding.

