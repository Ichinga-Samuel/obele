# obele Documentation

This directory documents the current `obele` package as implemented in this repository.

`obele` has two main subsystems:

- `obele.orm`: the SQLite ORM
- `obele.kv`: the single-table persistent key-value store

Both subsystems are also re-exported from the top-level package where appropriate.

## Documentation Map

- [Tutorial and Guide](./tutorial.md)
- [API Reference](./API_REFERENCE.md)
- [Implementation Notes](./IMPLEMENTATION_NOTES.md)

## Package Summary

### `obele.orm`

The ORM provides:

- shared SQLite connection management
- scoped database bindings
- transaction helpers
- declarative models and typed fields
- schema-sync migrations
- relation traversal, joins, subqueries, and annotations
- sync and async APIs

Typical import:

```python
from obele import Database, Model, TextField, IntegerField
```

Equivalent subpackage import:

```python
from obele.orm import Database, Model, TextField, IntegerField
```

### `obele.kv`

The key-value layer provides:

- `KVStore`, a dict-like persistent mapping
- ordered and sliceable key mode by default
- batch reads and writes
- JSON or pickle serialization
- `KV`, a singleton global store wrapper

Typical import:

```python
from obele import KVStore, KV
```

Equivalent subpackage import:

```python
from obele.kv import KVStore, KV
```

## Repository Layout

```text
obele/
  __init__.py        Public top-level exports
  orm/
    __init__.py      ORM exports
    __main__.py      `python -m obele.orm`
    cli.py           Migration CLI
    database.py      SQLite connection layer
    exceptions.py    ORM exception hierarchy
    fields.py        Field descriptors and type conversion
    model.py         Model base class and migrations
    query.py         QuerySet, Q, and expression system
  kv/
    __init__.py      KV exports
    globals.py       Singleton `KV`
    store.py         `KVStore` implementation
tests/
  test_orm.py
  test_async_orm.py
  test_orm_enhancements.py
  test_async_orm_enhancements.py
  test_orm_cli.py
  test_kv.py
  test_kv_async.py
```

## Recommended Reading Order

1. Read the project [README](../README.md) for setup and examples.
2. Read [Tutorial and Guide](./tutorial.md) for end-to-end examples and mini-projects.
3. Read [API Reference](./API_REFERENCE.md) for public classes, methods, and options.
4. Read [Implementation Notes](./IMPLEMENTATION_NOTES.md) for SQLite-specific behavior, tradeoffs, and caveats.

## Feature Snapshot

The current repository includes coverage for:

- schema creation and teardown
- schema-sync migrations and migration CLI
- CRUD flows
- relation traversal and eager loading
- reverse relations
- `Q` expressions, subqueries, joins, and annotations
- sync and async APIs
- key-value CRUD, slicing, range queries, and singleton usage

## Scope

The project remains intentionally focused:

- SQLite only
- standard library only at runtime
- async wrappers over sync `sqlite3`
- schema-sync migrations without a migration history ledger
- expression-based query building rather than a backend-independent compiler

