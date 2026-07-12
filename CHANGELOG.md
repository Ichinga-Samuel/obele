# Changelog

## 1.0.0 (2026-07-12)

Major rewrite focused on correctness, features, efficiency, and a much
smaller codebase (~30% fewer lines while adding features). This release
contains breaking changes.

### Architecture

- The vendored `obele.asqlite` package is gone. Async APIs now run the sync
  implementation on a worker thread (`asyncio.to_thread`); context variables
  propagate, so scoped bindings and transactions behave identically in sync
  and async code. Async reads gained real parallelism (per-thread
  connections under WAL) instead of serializing on one shared connection.
- Transactions check out a dedicated connection pinned in a `ContextVar`;
  every statement inside the block - from any helper, sync or async - routes
  to it. Nested transactions become savepoints.
- Plain `":memory:"` databases are upgraded to a named shared-cache database
  held open by an anchor connection, so all threads and async tasks see the
  same data (previously each thread silently got its own empty database and
  the async path broke entirely on `:memory:`).
- A `REGEXP` implementation is registered on every connection, so the
  `field__regex` lookup works out of the box.

### Breaking changes

- Removed `obele.asqlite`, `async_connect`, `AsyncSQLiteConnection`, and
  `AsyncSQLiteCursor`.
- `Database.aexecute()` / `aexecutemany()` return a materialized
  `ExecResult` (`rows`, `rowcount`, `lastrowid`) instead of an async cursor;
  there is nothing to `close()`.
- `Database.configure()` no longer accepts `pool_size` / `max_connection_age`;
  `pool_status()` is replaced by `status()`.
- `Database.transaction()` yields the underlying `sqlite3.Connection`
  (instead of a wrapper) and accepts a `mode` argument
  (`DEFERRED` / `IMMEDIATE` / `EXCLUSIVE`).
- `Database.aclose()` and `Database.aget_connection()` are removed; use
  `Database.aclose_all()`.
- `DateTimeField` no longer silently defaults to `datetime.now` - pass
  `default=datetime.datetime.now` explicitly. (This also fixes
  `SoftDeleteMixin.deleted_at` being set to "now" on creation.)
- Integer primary keys no longer use `AUTOINCREMENT` (plain
  `INTEGER PRIMARY KEY` is the SQLite-recommended rowid alias).
- `Model.aggregate("SUM", ...)`-style calls are unchanged, but bare strings
  passed to expression functions (`Sum("amount")`) now correctly reference
  the column instead of binding a literal string.
- Chaining `filter()` / `annotate()` / `values()` after a set operation
  (`union` / `intersection` / `difference`) raises `ValueError` instead of
  silently ignoring the call; `order_by` / `limit` / `offset` now work on
  combined queries.
- `KVStore` no longer auto-migrates pre-namespace (0.0.x) tables.
- `execute_script()` raises inside a transaction (SQLite would implicitly
  commit it).

### Fixed

- `prefetch_related()` cache is now actually used: prefetched reverse
  managers serve `all()` / `count()` / iteration without extra queries
  (previously it stored the cache and then ignored it, hitting the database
  anyway).
- `KVStore.prefix("")` / `prefix_count("")` / `prefix_delete("")` now match
  all string keys (previously returned nothing).
- `KVStore.apop()` no longer calls sync database code on the event loop and
  correctly round-trips stored `None` values.
- Sync/async divergence bugs eliminated wholesale: every async method is now
  a thin delegate of its sync twin.
- UPDATE/DELETE statements use the primary key's column name (previously the
  attribute name, which broke custom `column_name` PKs).

### Added

- `Model.pk` property (read/write, independent of the PK field name).
- QuerySet: lazy slicing (`qs[:10]`, `qs[3]`), `last()`, `latest()`,
  `earliest()`, `in_bulk()`, and async twins.
- Expressions: `F` arithmetic (`F("views") + 1`), expression values in
  `update()`, `Count()` with no arguments (`COUNT(*)`).
- Model class-level QuerySet proxy: every QuerySet method is reachable as
  `User.order_by(...)`, `await User.aall()`, etc.
- Schema helpers: `create_all()`, `drop_all()`, `registered_models()` (and
  async twins) using FK-dependency ordering.
- Fields: `choices=` on every field, `min_value` / `max_value` on
  `IntegerField` / `RealField`, `on_delete` validation on `ForeignKeyField`.
- `Database`: `transaction(mode=...)`, `tables()`, `vacuum()`, `status()`,
  value-safety validation on `pragma()` writes.
- `KVStore.memoize(ttl=...)` decorator caching sync or async function
  results, and `increment()` starting from zero for missing keys.
- `bulk_create()` uses chunked multi-row `INSERT ... RETURNING` (about 40%
  faster) while preserving input order.

### Performance (vs 0.1.0, in-memory benchmarks)

- `get_by_pk`: ~2.9x faster; instance `save()` updates: ~2.3x faster.
- `create`: ~27% faster; `bulk_create`: ~40% faster; KV writes: ~25% faster.
- Test suite runs in roughly half the time.
