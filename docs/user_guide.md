# User Guide

This guide covers the core Obele APIs: database configuration, models, queries,
async usage, relationships, full-text search, and the built-in key-value store.

## Database Setup

Configure the global database once at application startup:

```python
from obele import Database

Database.configure("app.sqlite3")
```

If no path is configured, Obele uses `DB.sqlite3` or the `OBELE_DATABASE`
environment variable when present.

You can pass SQLite pragmas and connection options:

```python
Database.configure(
    "app.sqlite3",
    pragmas={"cache_size": -16000},
    pool_size=10,
    max_connection_age=300,
    log_queries=True,
    slow_query_threshold=0.5,
)
```

Async configuration and cleanup are available too:

```python
await Database.aconfigure("app.sqlite3")
await Database.aclose_all()
```

## Defining Models

Models subclass `Model` and declare field instances as class attributes.

```python
from obele import Model, TextField, IntegerField, BooleanField

class Product(Model):
    table_name = "products"

    name = TextField()
    price = IntegerField()
    is_active = BooleanField(default=True)
```

Create the table before inserting data:

```python
Product.create_table()
await Product.acreate_table()
```

## Creating And Updating Rows

```python
product = Product.create(name="Laptop", price=1200)
product.price = 999
product.save()
```

Async methods are prefixed with `a`:

```python
product = await Product.acreate(name="Laptop", price=1200)
product.price = 999
await product.asave()
```

Use `refresh()` when a row may have changed outside the current model object:

```python
product.refresh()
await product.arefresh()
```

## Queries

Obele uses a lazy `QuerySet` API.

```python
Product.filter(is_active=True)
Product.filter(price__gte=500, price__lte=1500)
Product.filter(name__icontains="book")
Product.exclude(name="Archived")
Product.order_by("-price", "name").limit(10).offset(20)
```

Materialize results with terminal methods:

```python
products = Product.filter(is_active=True).all()
first = Product.order_by("name").first()
count = Product.filter(price__gt=1000).count()
exists = Product.filter(name="Laptop").exists()
```

Async query methods use the same API:

```python
products = await Product.filter(is_active=True).aall()
first = await Product.order_by("name").afirst()
count = await Product.filter(price__gt=1000).acount()
deleted = await Product.filter(is_active=False).adelete()
```

For streaming large result sets:

```python
for product in Product.order_by("id").iterator(chunk_size=500):
    ...

async for product in Product.order_by("id").aiterator(chunk_size=500):
    ...
```

## Relationships

Use `ForeignKeyField` for one-to-many relationships. The `related_name`
argument creates the reverse relation.

```python
from obele import Model, TextField, ForeignKeyField

class Category(Model):
    table_name = "categories"
    name = TextField(unique=True)

class Item(Model):
    table_name = "items"
    name = TextField()
    category = ForeignKeyField(to=Category, related_name="items")
```

```python
category = Category.create(name="Hardware")
Item.create(name="Keyboard", category=category)

items = category.items.all()
same_items = Item.filter(category__name="Hardware").all()
```

Use `select_related()` when you want to hydrate a direct foreign key in the
same query:

```python
item = Item.select_related("category").first()
print(item.category.name)
```

## Transactions

Transactions support both sync and async context managers. Nested transactions
use SQLite savepoints.

```python
with Database.transaction():
    Product.create(name="One", price=1)
    Product.create(name="Two", price=2)

async with Database.transaction():
    await Product.acreate(name="Async One", price=1)
    await Product.acreate(name="Async Two", price=2)
```

## Scoped Databases

`Database.using()` temporarily binds operations to another database path. This
is useful in tests, scripts, and multi-tenant workflows.

```python
with Database.using("tenant.sqlite3"):
    Product.create_table()
    Product.create(name="Tenant Product", price=100)

async with Database.using("tenant.sqlite3"):
    await Product.acreate_table()
```

## Direct SQL

Use direct SQL helpers when the ORM is not the right fit.

```python
cursor = Database.execute(
    "INSERT INTO products (name, price) VALUES (?, ?)",
    ["Laptop", 1200],
)

row = Database.fetchone("SELECT * FROM products WHERE id = ?", [cursor.lastrowid])
rows = Database.fetchall("SELECT * FROM products")
```

Async direct SQL uses the bundled async SQLite bridge and returns async cursors:

```python
cursor = await Database.aexecute(
    "INSERT INTO products (name, price) VALUES (?, ?)",
    ["Laptop", 1200],
)
await cursor.close()

row = await Database.afetchone("SELECT * FROM products WHERE id = ?", [1])
rows = await Database.afetchall("SELECT * FROM products")
```

For raw async SQLite access, use `async_connect`:

```python
from obele import async_connect

async with async_connect("raw.sqlite3") as connection:
    cursor = await connection.execute("SELECT 1")
    await cursor.close()
```

## Pagination

```python
page = Product.order_by("id").paginate(page=1, per_page=20)
async_page = await Product.order_by("id").apaginate(page=1, per_page=20)
```

Cursor pagination is useful for large ordered datasets:

```python
page = Product.order_by("id").cursor_paginate(per_page=20)
next_page = Product.order_by("id").cursor_paginate(
    per_page=20,
    after=page.end_cursor,
)
```

## Full-Text Search

`SearchIndex` creates an SQLite FTS5 table tied to a model.

```python
from obele import SearchIndex

idx = SearchIndex(Product, fields=["name"])
idx.create()

Product.create(name="Python Handbook", price=25)
idx.rebuild()

results = idx.search("python")
count = idx.search_count("python")
```

Async equivalents are available:

```python
await idx.acreate()
await idx.arebuild()
results = await idx.asearch("python")
```

## Mixins

`TimestampMixin` adds `created_at` and `updated_at`.

```python
from obele import TimestampMixin, SoftDeleteMixin

class Article(TimestampMixin, SoftDeleteMixin, Model):
    table_name = "articles"
    title = TextField()
```

`SoftDeleteMixin` makes `delete()` mark rows as deleted instead of removing
them from default queries. Use `with_deleted()`, `only_deleted()`, `restore()`,
or `hard_delete()` when needed.

## Signals

Signals let you hook into model lifecycle events:

```python
from obele import post_save, receiver

@receiver(post_save, sender=Product)
def on_product_saved(sender, instance, created):
    print(instance.name, created)
```

## Key-Value Store

`KVStore` is a persistent mapping backed by SQLite.

```python
from obele import KVStore

store = KVStore("settings", key_type=str, namespace="app")
store["theme"] = "dark"
store.set("session", {"user_id": 1}, ttl=3600)

assert store["theme"] == "dark"
```

It supports batch operations, TTL, prefix/range/scan queries, atomic helpers,
serializers, namespaces, and async methods:

```python
await store.aset("feature:search", True)
enabled = await store.aget("feature:search")

await store.aset_many({"a": 1, "b": 2})
items = await store.aitems()
```

## Testing

Install development dependencies and run:

```bash
python -m pip install -e ".[dev]"
pytest -q
```

The suite is organized by public behavior under `tests/`, and pytest is
configured to import the local checkout.
