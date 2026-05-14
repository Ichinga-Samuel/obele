# User Guide

This guide covers the core features of the Obele ORM and how to use them.

## Defining Models

Models are defined by subclassing `obele.Model` and specifying `obele.Field` instances as class attributes.

```python
from obele import Model, TextField, IntegerField, BooleanField

class Product(Model):
    table_name = "products" # Optional: defaults to lowercase class name
    
    name = TextField()
    price = IntegerField()
    is_active = BooleanField(default=True)
```

## Making Queries

Obele uses a `QuerySet` API similar to Django.

### Filtering and Lookups

```python
# Exact match
Product.filter(name="Laptop")

# Lookups (e.g., greater than, less than)
Product.filter(price__gt=1000)
Product.filter(price__lte=500)
Product.filter(name__startswith="Apple")
Product.filter(name__icontains="book")

# Multiple conditions (AND)
Product.filter(is_active=True, price__lt=2000)
```

### Ordering and Limiting

```python
Product.filter(is_active=True).order_by("-price", "name").limit(10).offset(20)
```

### Async Queries

Every terminating `QuerySet` method has an asynchronous counterpart prefixed with `a`:

```python
await Product.filter(is_active=True).aall()
await Product.filter(id=1).afirst()
await Product.filter(name="Laptop").acount()
await Product.filter(price__lt=500).adelete()
```

## Relationships

Use `ForeignKeyField` to define one-to-many relationships.

```python
from obele import Model, TextField, ForeignKeyField

class Category(Model):
    name = TextField()

class Item(Model):
    name = TextField()
    category = ForeignKeyField(Category, backref="items")
```

You can then access related objects:

```python
category = Category.get(id=1)

# Fetch related items
items = category.items.all()

# Add a related item
new_item = Item.create(name="Smartphone", category=category)
```

## Advanced Features

Obele provides several advanced features out of the box:

- **SoftDeleteMixin**: Add `is_deleted` and `deleted_at` fields to your model, and automatically filter out deleted records.
- **TimestampMixin**: Automatically manage `created_at` and `updated_at` timestamps.
- **Signals**: Use `@pre_save`, `@post_save`, `@pre_delete`, etc., to execute code around model lifecycle events.
- **SearchIndex**: Create FTS5 virtual tables to perform fast full-text searches across your data.
- **KVStore**: Use `obele.KVStore` or `obele.KV` for a simple key-value document store.
