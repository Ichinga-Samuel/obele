# Obele

Obele is a lightweight SQLite ORM and key-value store for modern Python
applications. It provides a small, Django-like model and query API. Every
method has an async twin (`aall`, `acreate`, ...) that runs on a worker
thread, so transactions and scoped bindings behave identically in sync and
async code.

## Install

```bash
pip install obele
```

For development:

```bash
python -m pip install -e ".[dev]"
pytest -q
```

## Quickstart

```python
import asyncio
from obele import Database, Model, TextField, IntegerField

Database.configure("my_app.sqlite3")

class User(Model):
    table_name = "users"
    name = TextField()
    age = IntegerField(nullable=True)

async def main():
    await User.acreate_table()
    await User.acreate(name="Alice", age=30)
    await User.acreate(name="Bob", age=25)

    adults = await User.filter(age__gte=18).order_by("name").aall()
    for user in adults:
        print(f"{user.name} is {user.age} years old")

    await Database.aclose_all()

asyncio.run(main())
```

Synchronous code uses the same methods without the `a` prefix:

```python
User.create_table()
User.create(name="Ada", age=36)
users = User.filter(age__gte=18).all()
```

## Features

- Sync and async model, query, search, database, and KVStore APIs.
- Async SQL access via `Database.aexecute()`, `Database.afetchone()`, and
  `Database.afetchall()` - results come back fully materialized, with no
  cursors to manage.
- Thread-safe SQLite connection management with WAL mode, per-thread read
  connections, transactions with savepoint nesting, scoped bindings, and
  optional query logging.
- Declarative fields, validation, foreign keys, reverse relations,
  expressions (`F("views") + 1`), mixins, signals, full-text search, and
  pagination.
- Persistent `KVStore` with TTL, namespaces, atomic operations, serializers,
  prefix/range/scan queries, memoization, and async equivalents.

## Where To Go Next

- Read the [User Guide](user_guide.md) for examples and patterns.
- Read [Architecture and Execution Model](architecture.md) to understand how
  database routing, fields, models, queries, async work, and KV storage fit
  together.
- See the [Migration CLI](cli.md) for schema synchronization.
- Browse the [ORM API](api/orm.md), [Query API](api/queries.md),
  [Fields API](api/fields.md), and [KVStore API](api/kv.md).
