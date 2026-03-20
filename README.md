# obele

[![PyPI version](https://img.shields.io/pypi/v/obele.svg)](https://pypi.org/project/obele/)
[![Python](https://img.shields.io/pypi/pyversions/obele.svg)](https://pypi.org/project/obele/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

`obele` is a SQLite-only data toolkit built on the Python standard library. It combines a lightweight ORM in `obele.orm`, a single-table key-value store in `obele.kv`, and convenient top-level imports for the main public API.

The project is intentionally pragmatic: no backend abstraction, no runtime dependencies, and no migration history ledger. It is built for applications that want a typed API over `sqlite3` without adopting a large framework.

## Installation

```bash
pip install obele
```

Development install:

```bash
pip install -e ".[dev]"
```

Requirements:

- Python 3.13+
- SQLite through the Python standard library

## Import Style

Top-level imports are re-exported for convenience:

```python
from obele import Database, Model, TextField, IntegerField, KVStore, KV
```

Equivalent subpackage imports:

```python
from obele.orm import Database, Model, TextField, IntegerField
from obele.kv import KVStore, KV
```

## Highlights

- declarative SQLite models with typed fields
- sync and async APIs with matching method names
- scoped database bindings and transaction contexts
- schema-sync migrations with a CLI
- relation traversal, joins, `Q` expressions, subqueries, and annotations
- hydrated `select_related()` and reverse relation managers
- validated bulk writes with optional fast-path validation bypass
- a fast dict-like key-value store with ordered key mode, slicing, and range queries
- a singleton global key-value store wrapper

## Quick Start

```python
from obele import Database, Model, TextField, IntegerField, BooleanField


Database.configure("app.sqlite3")


class User(Model):
    table_name = "users"

    name = TextField()
    email = TextField(unique=True)
    age = IntegerField(nullable=True)
    active = BooleanField(default=True)


User.create_table()

alice = User.create(name="Alice", email="alice@example.com", age=30)
users = User.filter(age__gte=18).order_by("name").all()

alice.active = False
alice.save()
```

## Async Example

The async API is implemented with `asyncio.to_thread()` around synchronous SQLite calls.

```python
import asyncio

from obele import Database, Model, TextField, IntegerField


class Task(Model):
    table_name = "tasks"
    title = TextField()
    priority = IntegerField(default=1)


async def main() -> None:
    await Database.aconfigure(":memory:")
    await Task.acreate_table()

    await Task.acreate(title="Ship docs", priority=2)

    async for task in Task.filter(priority__gte=1).order_by("title"):
        print(task.title)


asyncio.run(main())
```

## ORM Overview

### Database

`Database` manages the active SQLite connection and exposes:

- `configure()` / `aconfigure()`
- `using()` for temporary scoped bindings
- `transaction()` for multi-statement work
- `execute()` / `execute_read()` and async variants

Scoped binding example:

```python
from obele import Database


Database.configure("main.sqlite3")

with Database.using("other.sqlite3"):
    Database.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
```

Transaction example:

```python
with Database.transaction() as conn:
    conn.execute("INSERT INTO logs (message) VALUES (?)", ["started"])
    conn.execute("INSERT INTO logs (message) VALUES (?)", ["finished"])
```

### Models and Fields

```python
from obele import Model, TextField, IntegerField, DateTimeField


class Post(Model):
    table_name = "posts"

    title = TextField()
    body = TextField(nullable=True)
    score = IntegerField(default=0, index=True)
    created_at = DateTimeField(db_default="CURRENT_TIMESTAMP")
```

Supported base field options:

- `primary_key`
- `nullable`
- `default`
- `db_default`
- `unique`
- `index`
- `column_name`

`TextField` also supports `max_length`.

### CRUD

```python
post = Post.create(title="Hello")
post.score = 10
post.save()
post.refresh()
post.delete()
```

Serialization:

```python
python_values = post.to_dict()
db_values = post.to_db_dict()
```

`to_dict()` returns Python values. `to_db_dict()` returns SQLite-serialized values.

### Querying

Basic lookups:

```python
User.filter(name="Alice")
User.filter(age__gte=18)
User.filter(name__contains="ali")
User.filter(name__icontains="ALI")
User.filter(name__startswith="A")
User.filter(age__between=(18, 30))
User.filter(id__in=[1, 2, 3])
User.filter(name__not_in=["Alice", "Bob"])
User.filter(email__is_null=False)
```

Boolean query composition with `Q`:

```python
from obele import Q


User.filter(Q(name="Alice") | Q(age__lt=18))
User.filter(~Q(active=False))
```

Ordering and streaming:

```python
users = User.order_by("-age", "name").limit(10).offset(20).all()

for user in User.filter(active=True).iterator(chunk_size=500):
    print(user.name)
```

### Relations

```python
from obele import ForeignKeyField


class Author(Model):
    table_name = "authors"
    name = TextField()


class Article(Model):
    table_name = "articles"
    title = TextField()
    author = ForeignKeyField(to=Author, related_name="articles")
```

You can pass related instances directly:

```python
author = Author.create(name="Alice")
article = Article.create(title="Intro", author=author)
```

Eager loading:

```python
article = Article.select_related("author").get(title="Intro")
print(article.author.name)
```

Reverse relations:

```python
author.articles.count()
author.articles.create(title="Next article")
```

### Joins, Subqueries, and Annotations

```python
from obele import Count, F, Func, RawSQL, Subquery


adult_ids = Subquery(User.filter(age__gte=18), field="id")
posts = Article.filter(author__in=adult_ids)

ranked = (
    User.annotate(
        name_length=Func("LENGTH", F("name")),
        age_plus_one=RawSQL("users.age + 1"),
        article_count=Count(F("articles__id")),
    )
    .order_by("-article_count", "name")
    .all()
)
```

### Migrations

Schema changes are handled by schema-sync model migrations:

```python
User.migrate()
await User.amigrate()
```

Column rename example:

```python
User.migrate(rename_fields={"full_name": "name"})
```

There is no migration history ledger.

### Migration CLI

List discovered models:

```bash
python -m obele.orm list-models --module myapp.models
```

Run migrations:

```bash
python -m obele.orm migrate --database app.sqlite3 --module myapp.models
```

Installed entry point:

```bash
obele-orm migrate --database app.sqlite3 --module myapp.models
```

Explicit rename mapping:

```bash
obele-orm migrate \
  --database app.sqlite3 \
  --module myapp.models \
  --rename UserProfile.full_name=name
```

## Key-Value Store

`KVStore` is a single-table persistent mapping built on the same SQLite database layer.

```python
from obele import Database, KVStore


Database.configure("app.sqlite3")
store = KVStore("settings", key_type=str)

store["theme"] = "dark"
store["language"] = "en"

assert store["theme"] == "dark"
assert "theme" in store
assert len(store) == 2
```

Ordered key mode is enabled by default. When key enforcement is on:

- all keys share one sortable type
- ordered iteration is stable
- slicing and range queries are available

Examples:

```python
store = KVStore("scores", key_type=int)
store.set_many({1: "one", 2: "two", 3: "three"})

subset = store[1:3]
pairs = store.range(1, 4, return_type="tuple")
many = store.get_many(1, 3, return_type="dict")
```

Mixed-type keys can be enabled explicitly:

```python
store = KVStore("mixed", enforce_key_type=False)
```

Serialization modes:

- `"auto"`: JSON first, pickle fallback
- `"json"`: JSON only
- `"pickle"`: pickle only
- `(dumps, loads)`: custom serializer pair

Async KV methods are also available, such as `aget()`, `aset()`, `aget_many()`, `aset_many()`, and `aclear()`.

### Global Singleton KV

`KV` is a process-wide singleton subclass of `KVStore`:

```python
from obele import KV


settings = KV("app_settings", key_type=str)
settings["theme"] = "dark"

same_settings = KV("ignored_name")
assert same_settings is settings

KV.reset()
fresh_settings = KV("new_settings", key_type=str)
```

The first `KV(...)` call uses the constructor arguments. Later calls return the same object and ignore new arguments until `KV.reset()` is called.

## Package Layout

```text
obele/
  __init__.py
  orm/
    __init__.py
    __main__.py
    cli.py
    database.py
    exceptions.py
    fields.py
    model.py
    query.py
  kv/
    __init__.py
    globals.py
    store.py
docs/
  README.md
  API_REFERENCE.md
  IMPLEMENTATION_NOTES.md
tests/
  test_orm.py
  test_async_orm.py
  test_orm_enhancements.py
  test_async_orm_enhancements.py
  test_orm_cli.py
  test_kv.py
  test_kv_async.py
```

## Current Scope

`obele` is intentionally:

- SQLite-only
- expression-oriented rather than backend-agnostic
- async-friendly, but still backed by synchronous `sqlite3`
- schema-sync migration based, without a migration ledger

Practical caveats:

- `select_related()` currently supports direct foreign keys only
- `REGEXP` lookups require a SQLite `REGEXP` function to be registered if you intend to use them
- `KV` is process-local singleton state, not a distributed shared cache

## Documentation

Additional reference material is available in:

- [`docs/tutorial.md`](docs/tutorial.md)
- [`docs/tutorial_2.md`](docs/tutorial_2.md)
- [`docs/README.md`](docs/README.md)
- [`docs/API_REFERENCE.md`](docs/API_REFERENCE.md)
- [`docs/IMPLEMENTATION_NOTES.md`](docs/IMPLEMENTATION_NOTES.md)

## Development

Run the full test suite with:

```bash
pytest
```

The repository includes coverage for ORM basics, enhanced queries, migrations, async behavior, CLI migrations, and key-value storage.

## License

[MIT](LICENSE)

