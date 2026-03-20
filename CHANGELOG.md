# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2025-03-18

### Added

- Core ORM with `Model` base class and `MetaModel` metaclass
- Field types: `IntegerField`, `TextField`, `RealField`, `BooleanField`, `DateTimeField`, `BlobField`, `ForeignKeyField`
- Field options: `nullable`, `unique`, `default`, `index`, `primary_key`, `max_length`
- Full CRUD: `create`, `save`, `delete`, `refresh`, `get_or_create`, `bulk_create`
- `QuerySet` with fluent chaining: `filter`, `exclude`, `order_by`, `limit`, `offset`
- Lookup operators: `exact`, `gt`, `gte`, `lt`, `lte`, `ne`, `like`, `in`, `is_null`
- Aggregates: `SUM`, `AVG`, `MIN`, `MAX`, `COUNT`
- Bulk mutations: `update`, `delete` on querysets
- `select_related` for foreign key joins
- Thread-safe `Database` with serialised writes and concurrent reads
- Complete async API (`acreate`, `asave`, `aall`, `aget`, etc.) via `asyncio.to_thread`
- Async iteration (`async for`) on querysets
- Async/sync context manager support on `Database`
- Environment variable configuration (`OBELE_DATABASE`)
- Custom exception hierarchy: `ORMError`, `FieldValidationError`, `RecordNotFoundError`, `MultipleResultsError`, `DatabaseError`, `IntegrityError`

