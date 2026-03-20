# Obele: The Zero-Dependency Python ORM That Does More Than You'd Expect

**Build async-ready SQLite apps with a clean, Pythonic API â€” no dependencies required.**

---

SQLite is the most deployed database engine on the planet. It ships with Python, needs zero configuration, and handles more workloads than most developers give it credit for.

**Obele** is a lightweight, async-ready ORM built *specifically* for SQLite, with zero external dependencies. 

It ships model-based CRUD, a composable query builder, transaction management, schema migrations, a built-in key-value store,

and a CLI all in a single `pip install`.

In this tutorial, we'll build a real application step by step, exploring every major feature along the way.

---

## Table of Contents

1. [Installation & Setup](#installation--setup)
2. [Defining Models](#defining-models)
3. [CRUD Operations](#crud-operations)
4. [Querying with the Fluent API](#querying-with-the-fluent-api)
5. [Q Objects â€” Composable Filters](#q-objects--composable-filters)
6. [Expressions: F, Value, RawSQL, and Aggregates](#expressions-f-value-rawsql-and-aggregates)
7. [Relationships & select_related](#relationships--select_related)
8. [Reverse Relations](#reverse-relations)
9. [Annotations & Aggregation](#annotations--aggregation)
10. [Transactions & Savepoints](#transactions--savepoints)
11. [Scoped Databases](#scoped-databases)
12. [Schema Migrations](#schema-migrations)
13. [Async Support](#async-support)
14. [Streaming Large Result Sets](#streaming-large-result-sets)
15. [The Key-Value Store](#the-key-value-store)
16. [The Global Singleton KV](#the-global-singleton-kv)
17. [CLI Tooling](#cli-tooling)
18. [Conclusion](#conclusion)

---

## Installation & Setup

Obele requires **Python 3.13+** and has **zero dependencies**:

```bash
pip install obele
```

That's it. No database drivers to install, no C extensions to compile, no configuration files to write.

---

## Defining Models

Models are Python classes that inherit from `Model`. Each class attribute becomes a database column:

```python
from obele import Database, Model, TextField, IntegerField, BooleanField

# Point at your database
Database.configure("blog.sqlite3")

class Author(Model):
    table_name = "authors"
    name = TextField(max_length=100)
    bio  = TextField(nullable=True)

class Post(Model):
    table_name = "posts"
    title     = TextField()
    body      = TextField()
    published = BooleanField(default=False)
    views     = IntegerField(default=0)

# Create tables
Author.create_table()
Post.create_table()
```

A few things happen automatically:

- **An `id` primary key** is added if you don't declare one (auto-incrementing integer).
- **`table_name`** defaults to the lowercase class name if you omit it.
- **Validation** is enforced when you set field values â€” try assigning `None` to a non-nullable field and you'll get a `FieldValidationError` immediately.

### Available Field Types

| Field | SQLite Type | Python Type | Notes |
|---|---|---|---|
| `IntegerField` | INTEGER | `int` | Auto-increments when used as PK |
| `TextField` | TEXT | `str` | Optional `max_length` validation |
| `RealField` | REAL | `float` | Accepts `int` and `float` inputs |
| `BlobField` | BLOB | `bytes` | â€” |
| `BooleanField` | INTEGER | `bool` | Stored as 0/1, exposed as `True`/`False` |
| `DateTimeField` | TEXT | `datetime` | ISO-8601 format in the database |
| `ForeignKeyField` | INTEGER | `int` | With `on_delete`, `related_name` support |

Every field supports `nullable`, `default`, `unique`, `index`, `column_name`, and `db_default` (for raw SQL defaults like `CURRENT_TIMESTAMP`).

---

## CRUD Operations

### Create

```python
alice = Author.create(name="Alice", bio="Writes about Python")
# <Author pk=1>
```

Or build-then-save:

```python
bob = Author(name="Bob")
bob.save()
# bob.id is now set
```

### Read

```python
author = Author.get(name="Alice")
first  = Author.first()
all_   = Author.all()
```

### Update

```python
alice.bio = "Writes about Python and SQLite"
alice.save()
```

### Delete

```python
alice.delete()
```

### Bulk Create

Insert many rows in a single `executemany` call:

```python
authors = Author.bulk_create([
    {"name": "Carol"},
    {"name": "Dave"},
    {"name": "Eve"},
])
# Returns a list of saved instances with PKs
```

Bulk create validates every row by default. Bypass validation for trusted data:

```python
Author.bulk_create(items, validate=False)
```

### get_or_create

```python
author, created = Author.get_or_create(
    name="Frank",
    defaults={"bio": "New here"}
)
# created is True the first time, False after that
```

---

## Querying with the Fluent API

`QuerySet` is obele's chainable, lazy query builder. Nothing hits the database until you materialise results with `.all()`, `.first()`, `.count()`, or iteration:

```python
senior_authors = (
    Author
    .filter(name__startswith="A")
    .exclude(bio__is_null=True)
    .order_by("-name")
    .limit(10)
    .all()
)
```

### Rich Lookup Operators

Obele ships with a comprehensive set of lookups:

| Lookup | SQL Generated | Example |
|---|---|---|
| `exact` (default) | `= ?` | `name="Alice"` |
| `ne` | `!= ?` | `age__ne=18` |
| `gt`, `gte`, `lt`, `lte` | `>`, `>=`, `<`, `<=` | `age__gte=21` |
| `in` | `IN (?, ?, ...)` | `id__in=[1, 2, 3]` |
| `not_in` | `NOT IN (...)` | `id__not_in=[4, 5]` |
| `is_null` | `IS NULL` / `IS NOT NULL` | `bio__is_null=True` |
| `between` / `range` | `BETWEEN ? AND ?` | `age__between=(18, 65)` |
| `contains` | `LIKE '%...%'` | `name__contains="lic"` |
| `startswith` | `LIKE '...%'` | `name__startswith="Al"` |
| `endswith` | `LIKE '%...'` | `name__endswith="ce"` |
| `icontains` | case-insensitive `LIKE` | `name__icontains="ALICE"` |
| `iexact` | case-insensitive `=` | `name__iexact="alice"` |
| `like` | `LIKE ?` (raw) | `name__like="%ob%"` |
| `glob` | `GLOB ?` | `name__glob="A*"` |
| `regex` | `REGEXP ?` | `name__regex="^A.+e$"` |

All `LIKE`-based lookups automatically escape `%`, `_`, and `\` in your values. No SQL injection worries.

---

## Q Objects â€” Composable Filters

When you need `OR` logic or complex boolean combinations, use `Q`:

```python
from obele import Q

# OR: authors named Alice or Bob
results = Author.filter(
    Q(name="Alice") | Q(name="Bob")
).all()

# NOT: everyone who is NOT under 18
adults = Author.filter(~Q(age__lt=18)).all()

# Complex: (name starts with A AND has bio) OR (name is Bob)
results = Author.filter(
    (Q(name__startswith="A") & Q(bio__is_null=False)) | Q(name="Bob")
).all()
```

`Q` objects support `&` (AND), `|` (OR), and `~` (NOT), and they nest to arbitrary depth.

---

## Expressions: F, Value, RawSQL, and Aggregates

Obele has a full expression framework for referencing fields, injecting values, and writing raw SQL within the query builder:

### F â€” Field References

Use `F` to reference a column's value in a filter or annotation:

```python
from obele import F

# Posts where views > likes (comparing two columns)
Post.filter(views__gt=F("likes"))
```

### Value â€” Literal Bindings

```python
from obele import Value

# Annotate every row with a fixed value
Post.annotate(constant=Value(42)).all()
```

### RawSQL â€” Escape Hatch

For anything SQLite supports that the ORM doesn't wrap:

```python
from obele import RawSQL

Post.annotate(
    title_length=RawSQL("LENGTH(posts.title)")
).order_by("-title_length").all()
```

### Func â€” SQL Functions

```python
from obele import Func, F

Post.annotate(
    upper_title=Func("UPPER", F("title"))
).all()
```

### Built-in Aggregates

```python
from obele import Count, Sum, Avg, Min, Max

total_views = Post.aggregate("SUM", "views")
avg_views   = Post.aggregate("AVG", "views")
post_count  = Post.count()
```

Or with `.aggregate()` on a filtered queryset:

```python
popular_avg = Post.filter(views__gt=100).aggregate("AVG", "views")
```

---

## Relationships & select_related

Define foreign keys with `ForeignKeyField`:

```python
from obele import ForeignKeyField

class Post(Model):
    table_name = "posts"
    title  = TextField()
    author = ForeignKeyField(to=Author)
```

You can assign either a raw integer PK or a model instance:

```python
alice = Author.create(name="Alice")

# Both of these work:
post = Post.create(title="Hello", author=alice)       # model instance
post = Post.create(title="Hello", author=alice.id)    # integer PK
```

When you assign a model instance, obele **caches it** on the instance. The next time you access `post.author`, you get the full `Author` object without a second query:

```python
post.author          # â†’ <Author pk=1>  (cached, no extra query)
post.author.name     # â†’ "Alice"
```

### Eager Loading with select_related

Avoid N+1 queries by JOINing related objects upfront:

```python
posts = Post.select_related("author").all()

for post in posts:
    # No extra queries â€” author is already hydrated
    print(f"{post.title} by {post.author.name}")
```

---

## Reverse Relations

When you define a FK from `Post â†’ Author`, obele automatically installs a **reverse relation** on `Author`:

```python
alice = Author.get(name="Alice")

# Access posts via the reverse relation
alice.post_set.all()
alice.post_set.filter(published=True).count()
alice.post_set.order_by("-views").first()

# Create a post with the FK pre-set
alice.post_set.create(title="New Post", body="...")
```

Customize the accessor name with `related_name`:

```python
class Post(Model):
    author = ForeignKeyField(to=Author, related_name="posts")

# Now it's:
alice.posts.all()
```

The reverse relation manager supports the full QuerySet API: `.filter()`, `.exclude()`, `.order_by()`, `.count()`, `.exists()`, `.first()`, `.get()`, iteration, and async variants.

---

## Annotations & Aggregation

Annotations attach computed columns to your query results:

```python
from obele import Count, Avg, F, Func

users_with_stats = (
    Author
    .join("posts")
    .annotate(
        post_count=Count(F("posts__id")),
        avg_title_len=Avg(Func("LENGTH", F("posts__title"))),
    )
    .order_by("-post_count")
    .all()
)

for author in users_with_stats:
    print(f"{author.name}: {author.post_count} posts")
```

Annotations appear as attributes on the returned instances and are included in `.to_dict()`:

```python
author.to_dict()
# {'id': 1, 'name': 'Alice', 'bio': '...', 'post_count': 5, 'avg_title_len': 22.4}
```

---

## Transactions & Savepoints

Wrap multiple operations in an explicit transaction. On error, everything rolls back:

```python
try:
    with Database.transaction() as conn:
        Author.create(name="Temporary")
        raise ValueError("Oops!")
except ValueError:
    pass

# The author was never committed
Author.filter(name="Temporary").exists()  # False
```

Transactions **nest automatically** using SQLite savepoints:

```python
with Database.transaction():
    Author.create(name="Outer")
    try:
        with Database.transaction():  # â† savepoint
            Author.create(name="Inner")
            raise ValueError("Rollback inner only")
    except ValueError:
        pass
    # "Outer" is still committed; "Inner" was rolled back
```

---

## Scoped Databases

Need to temporarily point at a different database without mutating the global config? Use `Database.using()`:

```python
# Global config stays untouched
Database.configure("main.sqlite3")

with Database.using("analytics.sqlite3"):
    # All queries inside this block go to analytics.sqlite3
    path, _ = Database.current_config()
    assert path == "analytics.sqlite3"
    # ...

# Back to main.sqlite3 automatically
```

This is particularly useful in multi-tenant applications, test fixtures, or background jobs that need their own database.

---

## Schema Migrations

Obele includes a table-rebuild migration engine that handles column additions, removals, and renames:

```python
# v1 of your model
class User(Model):
    table_name = "users"
    name = TextField()

User.create_table()
User.create(name="Alice")

# v2 â€” add a column with a default
class User(Model):
    table_name = "users"
    name   = TextField()
    active = BooleanField(default=True, index=True)

User.migrate()  # Safely rebuilds the table, preserving data

user = User.get(name="Alice")
user.active  # True (from the default)
```

### Column Renames

```python
User.migrate(rename_fields={"full_name": "name"})
```

### db_default for SQL-Level Defaults

Need `CURRENT_TIMESTAMP` or other SQL expressions as defaults?

```python
from obele import DateTimeField

class Event(Model):
    table_name = "events"
    title      = TextField()
    created_at = DateTimeField(nullable=False, db_default="CURRENT_TIMESTAMP")

Event.migrate()
```

---

## Async Support

Every sync method has an `a`-prefixed async counterpart. They use `asyncio.to_thread()` under the hood, so your event loop never blocks:

```python
import asyncio
from obele import Database, Model, TextField

async def main():
    await Database.aconfigure(":memory:")

    class Note(Model):
        table_name = "notes"
        text = TextField()

    await Note.acreate_table()
    note = await Note.acreate(text="Hello async world!")

    all_notes = await Note.filter(text__contains="async").aall()
    print(all_notes)

    count = await Note.acount()
    first = await Note.afirst()
    note.text = "Updated!"
    await note.asave()
    await note.adelete()

    await Database.aclose()

asyncio.run(main())
```

Async methods available: `aconfigure`, `acreate_table`, `adrop_table`, `acreate`, `asave`, `adelete`, `arefresh`, `aall`, `afirst`, `aget`, `acount`, `aexists`, `aaggregate`, `aupdate`, `adelete`, `abulk_create`, `amigrate`, and more.

---

## Streaming Large Result Sets

Don't load a million rows into memory. Use `.iterator()` to stream results in chunks:

```python
for user in User.filter(active=True).iterator(chunk_size=500):
    process(user)
    # Only 500 rows in memory at a time
```

Async streaming works too:

```python
async for user in User.filter(active=True).aiterator(chunk_size=500):
    await process(user)
```

The default `__iter__` on a `QuerySet` uses `.iterator()` internally, so `for user in User.all_queryset:` also streams.

---

## The Key-Value Store

Obele includes a high-performance key-value store that works like a Python `dict` but persists in SQLite:

```python
from obele import Database, KVStore

Database.configure("app.sqlite3")
store = KVStore("settings", key_type=str)

# Dict-like interface
store["theme"] = "dark"
store["language"] = "en"
store["max_retries"] = 5

print(store["theme"])        # "dark"
print(len(store))            # 3
print("theme" in store)      # True

# Delete
del store["max_retries"]

# Iterate
for key, value in store.items():
    print(f"{key} = {value}")
```

### Range Queries via Slicing

When `key_type` is set (enforced by default), keys are sorted natively by SQLite. This enables **slice-based range queries**:

```python
store = KVStore("metrics", key_type=int)
for i in range(100):
    store[i] = f"value_{i}"

# Slice â€” returns a dict of keys in [10, 20)
subset = store[10:20]

# Step parameter controls max results
top_five = store[0:100:5]

# Open-ended slices
from_fifty = store[50:]
up_to_ten  = store[:10]
```

### Multi-Key Lookups

Fetch multiple keys in a single query:

```python
result = store.get_many("theme", "language", "missing_key")
# {"theme": "dark", "language": "en"}  (missing keys omitted)

# Or as tuples:
result = store.get_many("theme", "language", return_type="tuple")
# (("theme", "dark"), ("language", "en"))
```

### Batch Writes

Insert or update many key-value pairs atomically:

```python
store.set_many({
    "theme": "light",
    "font_size": 14,
    "sidebar": True,
})
```

### Pluggable Serialization

By default, obele tries JSON first and falls back to pickle for complex objects. You can force a mode or supply a custom serializer:

```python
# JSON only (fails on non-JSON-serializable values)
store = KVStore("config", serializer="json")

# Pickle only
store = KVStore("cache", serializer="pickle")

# Custom serializer
import msgpack
store = KVStore("fast", serializer=(msgpack.packb, msgpack.unpackb))
```

---

## The Global Singleton KV

For application-wide settings, use `KV` â€” a singleton wrapper around `KVStore`:

```python
from obele import KV

# First call creates the store
kv = KV("app_settings", key_type=str)
kv["theme"] = "dark"

# Subsequent calls return the SAME instance (args ignored)
kv2 = KV("this_is_ignored")
assert kv2["theme"] == "dark"
assert kv is kv2

# Reset to allow re-creation
KV.reset()
kv3 = KV("new_table")  # fresh store
```

This is thread-safe (double-checked locking) and ideal for global configuration stores.

---

## CLI Tooling

Obele includes a command-line interface for schema management:

```bash
# Run migrations for all models in a module
python -m obele.orm migrate \
    --database app.sqlite3 \
    --module myapp.models

# List discovered models
python -m obele.orm list-models \
    --module myapp.models

# Rename a column during migration
python -m obele.orm migrate \
    --database app.sqlite3 \
    --module myapp.models \
    --rename "User.email=old_email_column"

# Apply SQLite pragmas
python -m obele.orm migrate \
    --database app.sqlite3 \
    --module myapp.models \
    --pragma journal_mode=WAL \
    --pragma busy_timeout=5000
```

The CLI automatically discovers `Model` subclasses in your modules and runs them in **topological order** (respecting foreign key dependencies).

---

## Serialization Modes

Every model instance can be serialized in two modes:

```python
import datetime
from obele import DateTimeField, BooleanField

class Event(Model):
    table_name = "events"
    happened_at = DateTimeField()
    active      = BooleanField(default=True)

event = Event.create(
    happened_at=datetime.datetime(2025, 6, 15, 12, 30),
    active=True,
)

# Python mode â€” raw Python types
event.to_dict()
# {'id': 1, 'happened_at': datetime.datetime(2025, 6, 15, 12, 30), 'active': True}

# DB mode â€” SQLite-serialized values
event.to_db_dict()
# {'id': 1, 'happened_at': '2025-06-15T12:30:00', 'active': 1}
```

Annotations are included by default:

```python
user.to_dict()
# {'id': 1, 'name': 'Alice', 'post_count': 5}  â† annotation included

user.to_dict(include_annotations=False)
# {'id': 1, 'name': 'Alice'}
```

---

## Putting It All Together

Here's a complete mini-application showing many features working together:

```python
import asyncio
from obele import (
    Database, Model, TextField, IntegerField, BooleanField,
    ForeignKeyField, Q, F, Count, Func,
)

# --- Models ---

class User(Model):
    table_name = "users"
    username = TextField(unique=True)
    email    = TextField()
    active   = BooleanField(default=True, index=True)

class Tag(Model):
    table_name = "tags"
    name = TextField(unique=True)

class Article(Model):
    table_name = "articles"
    title   = TextField()
    body    = TextField()
    views   = IntegerField(default=0)
    author  = ForeignKeyField(to=User, related_name="articles")
    tag     = ForeignKeyField(to=Tag, related_name="articles", nullable=True)

# --- Setup ---

Database.configure(":memory:")
User.create_table()
Tag.create_table()
Article.create_table()

# --- Seed data ---

alice = User.create(username="alice", email="alice@example.com")
bob   = User.create(username="bob",   email="bob@example.com")

python_tag = Tag.create(name="Python")
sql_tag    = Tag.create(name="SQL")

# Create articles via reverse relation
alice.articles.create(title="Async Python", body="...", views=150, tag=python_tag)
alice.articles.create(title="SQLite Tips",  body="...", views=300, tag=sql_tag)
bob.articles.create(title="ORM Patterns",   body="...", views=80,  tag=python_tag)

# --- Queries ---

# Q objects with OR
popular_or_alice = Article.filter(
    Q(views__gt=100) | Q(author__username="alice")
).order_by("-views").all()

# Annotations with aggregates
authors_with_stats = (
    User
    .join("articles")
    .annotate(
        article_count=Count(F("articles__id")),
        total_views=Func("SUM", F("articles__views")),
    )
    .order_by("-total_views")
    .all()
)

for user in authors_with_stats:
    print(f"{user.username}: {user.article_count} articles, {user.total_views} views")
    # alice: 2 articles, 450 views
    # bob: 1 articles, 80 views

# select_related â€” no N+1
for article in Article.select_related("author").all():
    print(f"'{article.title}' by {article.author.username}")

# Subquery
top_authors = User.filter(active=True).limit(10)
top_articles = Article.filter(author__in=top_authors).all()

# Streaming
for article in Article.filter(views__gt=0).iterator(chunk_size=100):
    pass  # processes in chunks

# Serialization
article = Article.select_related("author").first()
print(article.to_dict())
print(article.to_db_dict())
```

---

## Why Obele?

| Feature | Obele | SQLAlchemy | Peewee |
|---|---|---|---|
| Zero dependencies | âœ… | âŒ | âŒ |
| SQLite-first | âœ… | âŒ (generic) | Partial |
| Async built-in | âœ… | Needs `aiosqlite` | âŒ |
| Q objects | âœ… | âœ… | âŒ |
| KV store | âœ… | âŒ | âŒ |
| Schema migrations | âœ… (built-in) | Needs Alembic | Needs `playhouse` |
| CLI tooling | âœ… | âŒ | âŒ |
| Transactions with savepoints | âœ… | âœ… | âœ… |
| Bundle size | ~50 KB | ~5 MB | ~500 KB |

Obele won't replace a full-featured ORM for a complex multi-database application. But if your project uses SQLite â€” and far more should â€” it gives you a **remarkably complete toolkit in a tiny, dependency-free package**.

---

## Get Started

```bash
pip install obele
```

ðŸ“¦ [GitHub](https://github.com/ichinga-samuel/obele) Â· ðŸ“„ [MIT License](https://github.com/ichinga-samuel/obele/blob/main/LICENSE)

---

*Built by [Samuel Ichinga](https://github.com/ichinga-samuel).*

