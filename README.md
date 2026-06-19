# Obele

Obele is a lightweight SQLite ORM and key-value store for modern Python
applications. It keeps a small, Django-like API for models and queries while
supporting both synchronous code and native asyncio workflows.

## Highlights

- Sync and async APIs for models, queries, raw SQL, search, and KV operations.
- Async SQLite integration through the bundled `obele.asqlite` bridge.
- Thread-safe SQLite connection management with transactions, savepoints,
  scoped database bindings, WAL mode, and query logging.
- Declarative models with field validation, foreign keys, reverse relations,
  mixins, signals, pagination, and FTS5 search indexes.
- Persistent dict-like `KVStore` with namespaces, TTL, atomic operations,
  range/prefix/scan queries, serializers, and async methods.

## Requirements

- Python 3.13 or newer.
- SQLite with FTS5 enabled for full-text search features.

Obele has no required runtime dependencies outside the Python standard library.

## Installation

```bash
pip install obele
```

For local development:

```bash
git clone https://github.com/ichinga-samuel/obele.git
cd obele
python -m pip install -e ".[dev]"
pytest -q
```

## Quickstart

```python
import asyncio
from obele import Database, Model, TextField, IntegerField

Database.configure("app.sqlite3")

class User(Model):
    table_name = "users"
    name = TextField()
    age = IntegerField(nullable=True)

async def main():
    await User.acreate_table()

    await User.acreate(name="Alice", age=30)
    await User.acreate(name="Bob", age=25)

    users = await User.filter(age__gte=18).order_by("name").aall()
    for user in users:
        print(user.name, user.age)

    await Database.aclose_all()

asyncio.run(main())
```

The same API is available synchronously by omitting the `a` prefix:

```python
User.create_table()
User.create(name="Ada", age=36)
adults = User.filter(age__gte=18).all()
```

## Direct SQL

Use `Database` helpers when you need lower-level access:

```python
cursor = Database.execute(
    "INSERT INTO users (name, age) VALUES (?, ?)",
    ["Grace", 40],
)
print(cursor.lastrowid)

row = Database.fetchone("SELECT name FROM users WHERE id = ?", [cursor.lastrowid])
```

Async helpers use the bundled async SQLite connection and return async cursors:

```python
cursor = await Database.aexecute(
    "INSERT INTO users (name, age) VALUES (?, ?)",
    ["Linus", 33],
)
await cursor.close()

row = await Database.afetchone("SELECT name FROM users WHERE age > ?", [30])
```

## Transactions And Scoped Databases

```python
with Database.transaction():
    User.create(name="One")
    User.create(name="Two")

async with Database.transaction():
    await User.acreate(name="Async One")
    await User.acreate(name="Async Two")
```

Use scoped bindings for tests, tenants, or short-lived alternate databases:

```python
with Database.using("tenant.sqlite3"):
    User.create_table()
    User.create(name="Tenant User")
```

## Key-Value Store

```python
from obele import KVStore

settings = KVStore("settings", key_type=str, namespace="app")
settings["theme"] = "dark"
settings.set("session", {"user_id": 1}, ttl=3600)

print(settings["theme"])
print(settings.prefix("sess"))
```

Async KV methods are prefixed with `a`:

```python
await settings.aset("feature:search", True)
enabled = await settings.aget("feature:search")
```

## Documentation

- [User Guide](docs/user_guide.md)
- [API Reference](docs/api/orm.md)
- [Queries](docs/api/queries.md)
- [Fields](docs/api/fields.md)
- [Key-Value Store](docs/api/kv.md)

To preview the docs locally:

```bash
mkdocs serve
```

## Testing

The test suite is organized by public behavior and uses the local checkout by
default via `pyproject.toml`.

```bash
pytest -q
```

Current suite coverage includes ORM CRUD, queries, async behavior, direct
database access, FTS search, pagination, signals, field types, and KVStore
features.

## License

Obele is released under the MIT License. See [LICENSE](LICENSE).
