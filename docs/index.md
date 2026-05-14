# Obele

A lightweight, async-ready ORM for SQLite, built for modern Python applications. 
Obele provides an intuitive, Django-like API while maintaining the simplicity and embedded nature of SQLite.

## Installation

```bash
pip install obele
```

## Quickstart

```python
import asyncio
from obele import Database, Model, TextField, IntegerField

# 1. Configure the database
Database.configure("my_app.db")

# 2. Define a model
class User(Model):
    name = TextField()
    age = IntegerField(nullable=True)

async def main():
    # 3. Create the table
    await User.acreate_table()

    # 4. Insert records
    await User.acreate(name="Alice", age=30)
    await User.acreate(name="Bob", age=25)

    # 5. Query records
    adults = await User.filter(age__gte=18).order_by("name").aall()
    for user in adults:
        print(f"{user.name} is {user.age} years old.")

if __name__ == "__main__":
    asyncio.run(main())
```
*(Note: Obele provides both sync and async APIs. You can omit the `a` prefix for synchronous usage: `User.create_table()`, `User.create()`, `User.filter().all()`)*

## Key Features

- **Sync and Async**: First-class support for both synchronous and asynchronous (asyncio) workflows.
- **Django-like API**: Familiar `Model`, `Field`, and `QuerySet` semantics.
- **Advanced Field Types**: Support for UUIDs, JSON, Enums, Timestamps, and more.
- **Relations**: Foreign keys and reverse relations out of the box.
- **Mixins**: Built-in support for Soft Deletion and Timestamping.
- **Signals**: Hook into pre/post save, create, and delete operations.
- **Pagination**: Offset/Limit and Cursor-based pagination.
- **Full-Text Search**: Built-in integration with SQLite FTS5.
- **Key-Value Store**: An integrated dict-like persistent KV store.

## Next Steps

- Read the [User Guide](user_guide.md) to learn how to use Obele effectively.
- Explore the [API Reference](api/orm.md) for detailed information on classes and methods.
