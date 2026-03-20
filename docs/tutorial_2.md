# obele Tutorial and Guide

This guide walks through `obele` with realistic mini-projects instead of isolated snippets. It covers the ORM in `obele.orm`, the key-value layer in `obele.kv`, and the current migration CLI.

## What You Will Build

The tutorial is organized around three practical use cases:

1. A small publishing platform using the ORM
2. A multi-tenant admin workflow using scoped databases and the migration CLI
3. Application settings, feature flags, and leaderboards using `KVStore` and `KV`

By the end, you will have seen:

- database configuration, scoped bindings, and transactions
- models, fields, CRUD, validation, and serialization
- relation traversal, reverse relations, eager loading, joins, and annotations
- sync and async APIs
- schema-sync migrations and the CLI
- dict-like key-value storage, slicing, range queries, multi-key reads, serialization modes, and singleton usage

## Project 1: Publishing Platform

This project models a blog-style application with authors, posts, and comments.

### Step 1: Configure the database

```python
from obele import Database


Database.configure("publishing.sqlite3")
```

### Step 2: Define the models

```python
from obele import (
    BooleanField,
    DateTimeField,
    ForeignKeyField,
    IntegerField,
    Model,
    TextField,
)


class Author(Model):
    table_name = "authors"
    name = TextField()
    email = TextField(unique=True)
    bio = TextField(nullable=True)
    active = BooleanField(default=True)


class Post(Model):
    table_name = "posts"
    title = TextField()
    slug = TextField(unique=True, index=True)
    body = TextField()
    published = BooleanField(default=False, index=True)
    views = IntegerField(default=0)
    author = ForeignKeyField(to=Author, related_name="posts")
    published_at = DateTimeField(nullable=True, db_default="CURRENT_TIMESTAMP")


class Comment(Model):
    table_name = "comments"
    post = ForeignKeyField(to=Post, related_name="comments")
    author = ForeignKeyField(to=Author, related_name="comments")
    body = TextField()
    approved = BooleanField(default=False, index=True)
```

This single model set already shows several important features:

- `TextField(unique=True)` for unique constraints
- `index=True` for query-heavy columns
- `ForeignKeyField(..., related_name=...)` for reverse access
- `db_default="CURRENT_TIMESTAMP"` for native SQLite defaults

### Step 3: Create or migrate the schema

For a first run, table creation is enough:

```python
Author.create_table()
Post.create_table()
Comment.create_table()
```

For an evolving application, schema-sync migrations are the better fit:

```python
Author.migrate()
Post.migrate()
Comment.migrate()
```

If you renamed a column:

```python
Post.migrate(rename_fields={"slug": "permalink"})
```

### Step 4: Create records

```python
alice = Author.create(name="Alice", email="alice@example.com", bio="Editor in chief")
bob = Author.create(name="Bob", email="bob@example.com")

welcome = Post.create(
    title="Welcome to the Magazine",
    slug="welcome",
    body="Our first article.",
    published=True,
    author=alice,
)

Comment.create(post=welcome, author=bob, body="Great launch!", approved=True)
```

Notes:

- you can pass related instances directly, not only raw foreign-key integers
- validation happens before inserts and updates

### Step 5: Read, update, delete, and serialize

```python
post = Post.get(slug="welcome")
post.views += 1
post.save()

python_values = post.to_dict()
db_values = post.to_db_dict()

post.refresh()
```

Use `to_dict()` for Python-facing data. Use `to_db_dict()` when you need SQLite-serialized values such as `1` for booleans or ISO strings for datetimes.

### Step 6: Filtering and lookups

```python
published_posts = Post.filter(published=True).order_by("-views", "title").all()
alice_posts = Post.filter(author__name="Alice").all()
search_hits = Post.filter(title__icontains="welcome").all()
popular = Post.filter(views__between=(100, 1000)).all()
non_drafts = Post.filter(slug__not_in=["draft-1", "draft-2"]).all()
```

Useful lookup families:

- comparison: `__gt`, `__gte`, `__lt`, `__lte`, `__ne`
- text: `__contains`, `__icontains`, `__startswith`, `__istartswith`, `__endswith`, `__iendswith`
- set and range: `__in`, `__not_in`, `__between`, `__range`
- null and pattern: `__is_null`, `__like`, `__glob`

### Step 7: `Q` expressions for more complex conditions

```python
from obele import Q


front_page = Post.filter(
    Q(published=True) & (Q(views__gte=1000) | Q(title__icontains="editorial"))
).all()

not_hidden = Post.filter(~Q(slug__startswith="internal-")).all()
```

### Step 8: Eager loading and reverse relations

Without eager loading, `post.author` is the raw foreign-key value. With `select_related()`, the related object is hydrated onto the field itself:

```python
post = Post.select_related("author").get(slug="welcome")
print(post.author.name)
```

Reverse relations give you queryset-like access from the other side:

```python
alice_post_count = alice.posts.count()
approved_comments = welcome.comments.filter(approved=True).all()
alice.posts.create(
    title="Behind the Scenes",
    slug="behind-the-scenes",
    body="How we run the publication.",
)
```

### Step 9: Joins, annotations, aggregates, and subqueries

Use annotations when you want computed columns attached to model instances.

```python
from obele import Count, F, Func, RawSQL, Subquery


active_author_ids = Subquery(Author.filter(active=True), field="id")

featured_posts = (
    Post.filter(author__in=active_author_ids)
    .annotate(
        title_length=Func("LENGTH", F("title")),
        views_plus_one=RawSQL("posts.views + 1"),
    )
    .order_by("-views_plus_one", "title")
    .all()
)
```

Aggregates across joins:

```python
authors = (
    Author.join("posts")
    .annotate(post_count=Count(F("posts__id")))
    .order_by("-post_count", "name")
    .all()
)
```

Plain aggregate helpers are also available:

```python
total_views = Post.filter(published=True).aggregate("SUM", "views")
average_views = Post.aggregate("AVG", "views")
```

### Step 10: Bulk operations

Bulk operations are useful for seed data and import tasks.

```python
Post.bulk_create(
    [
        {
            "title": "Issue One",
            "slug": "issue-one",
            "body": "Content",
            "author": alice,
            "published": True,
        },
        {
            "title": "Issue Two",
            "slug": "issue-two",
            "body": "More content",
            "author": bob,
        },
    ]
)

Post.filter(published=False).update(validate=True, views=0)
Comment.filter(approved=False).delete()
```

If you already trust the incoming data, validation can be skipped for throughput:

```python
Post.bulk_create(items, validate=False)
Post.filter(slug__startswith="import-").update(validate=False, published=True)
```

### Step 11: Transactions

Use a transaction when several writes must succeed or fail together.

```python
with Database.transaction() as conn:
    conn.execute("UPDATE posts SET views = views + 1 WHERE slug = ?", ["welcome"])
    conn.execute(
        "INSERT INTO comments (post, author, body, approved) VALUES (?, ?, ?, ?)",
        [welcome.id, bob.id, "Another note", 1],
    )
```

## Project 2: Multi-Tenant Admin Workflow

Imagine a SaaS product where every tenant gets its own SQLite database file.

### Per-tenant migrations

The CLI is the easiest way to migrate one tenant database at a time:

```bash
python -m obele.orm migrate --database tenants/acme.sqlite3 --module myapp.models
python -m obele.orm migrate --database tenants/zen.sqlite3 --module myapp.models
```

You can inspect which models the CLI will use:

```bash
python -m obele.orm list-models --module myapp.models
```

### Per-tenant reporting with `Database.using()`

```python
from pathlib import Path


tenant_paths = [
    Path("tenants/acme.sqlite3"),
    Path("tenants/zen.sqlite3"),
]

for path in tenant_paths:
    with Database.using(str(path)):
        published = Post.filter(published=True).count()
        comments = Comment.count()
        print(path.name, published, comments)
```

This pattern is useful for:

- maintenance scripts
- admin dashboards
- tenant health checks
- migration validation

### Async tenant jobs

```python
async def refresh_tenant_metrics(path: str) -> int:
    async with Database.using(path):
        return await Post.filter(published=True).acount()
```

## Project 3: Settings, Feature Flags, and Leaderboards

The KV layer is ideal when you want persistence without declaring a full ORM model for each tiny piece of data.

### Example A: Application settings

```python
from obele import Database, KVStore


Database.configure("app.sqlite3")
settings = KVStore("settings", key_type=str)

settings["site_name"] = "obele Weekly"
settings["moderation_enabled"] = True
settings["theme"] = {"primary": "#111111", "accent": "#ff5a36"}

site_name = settings["site_name"]
moderation_enabled = settings.get("moderation_enabled", False)
```

Batch access:

```python
snapshot = settings.get_many("site_name", "theme", return_type="dict")
settings.set_many({"timezone": "UTC", "posts_per_page": 20})
```

### Example B: Ordered leaderboard

Using integer keys gives you sortable ranges and slicing out of the box.

```python
scores = KVStore("leaderboard", key_type=int)
scores[1001] = {"user": "alice", "score": 920}
scores[1002] = {"user": "bob", "score": 870}
scores[1003] = {"user": "carol", "score": 990}

top_window = scores[1001:1004]
descending = scores.range(1001, 1004, reverse=True, return_type="tuple")
```

You can also project keys or values only:

```python
ids = scores.keys_slice(1001, 1004)
rows = scores.values_slice(1001, 1004)
```

### Example C: Mixed keys when ordering does not matter

```python
mixed = KVStore("misc_cache", enforce_key_type=False)
mixed["latest_post"] = "welcome"
mixed[("tenant", "acme")] = {"quota": 10}
```

This mode is useful for exact CRUD, but you should not depend on ordered range behavior.

### Example D: Global singleton settings store

`KV` gives you a process-wide singleton.

```python
from obele import KV


flags = KV("feature_flags", key_type=str)
flags["new_homepage"] = True

same_flags = KV("ignored_name")
assert same_flags is flags

KV.reset()
new_flags = KV("feature_flags_v2", key_type=str)
```

This is a good fit for:

- app settings
- feature flags
- lightweight in-process caches

It is not a distributed cache or cross-process coordination primitive.

## Serializer Choices

`KVStore` supports:

- `"auto"`: try JSON first, then pickle fallback
- `"json"`: force JSON-only values
- `"pickle"`: force pickle-only values
- custom `(dumps, loads)` serializer pairs

Use JSON when you want portability and human-readable data. Use pickle when you need arbitrary Python objects and you control the environment.

## Best Practices

- Configure the database once for the main application entry point.
- Use `Database.using()` for maintenance scripts, tenant loops, and isolated utilities.
- Prefer `Model.migrate()` or the migration CLI over ad-hoc DDL changes.
- Use `select_related()` when you know you need direct foreign-key objects.
- Use `iterator()` or `aiterator()` for large result sets.
- Keep KV stores homogeneous when you need ordering, slicing, or range queries.
- Use `KV.reset()` explicitly in tests when you need a fresh singleton store.

## Common Pitfalls

- `select_related()` currently supports direct foreign keys only.
- `REGEXP` lookups need a SQLite `REGEXP` function if you want to use them.
- Async APIs still use synchronous SQLite underneath.
- Schema migrations are ledger-free; the model definition is the source of truth.
- `KV(enforce_key_type=False)` disables predictable ordering semantics.

## Where To Go Next

- Use the project [README](../README.md) for a concise overview.
- Use [API_REFERENCE.md](./API_REFERENCE.md) for method-level reference.
- Use [IMPLEMENTATION_NOTES.md](./IMPLEMENTATION_NOTES.md) for behavior and caveats.

