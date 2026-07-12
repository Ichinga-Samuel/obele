# Architecture and Execution Model

This chapter explains how Obele works as a system. It follows data from a
model declaration through SQL generation, SQLite execution, row hydration,
and the asynchronous wrappers. The [source reference](internals.md) then
documents every module-level constant, helper, class, and method.

## The Big Picture

Obele is composed of four layers:

1. The database layer owns configuration, connections, transactions,
   concurrency, raw SQL, and SQLite maintenance.
2. The mapping layer uses field descriptors and a metaclass to turn Python
   classes into table definitions and model instances into rows.
3. The query layer stores a query as Python state, compiles that state into
   parameterized SQL, and converts result rows back into models or projected
   values.
4. Services such as relationships, pagination, FTS5 search, signals, mixins,
   migrations, and `KVStore` build on those three layers.

```text
Application code
   |-- Model / QuerySet --------> fields, expressions, relationships
   |-- KVStore -----------------> key/value encoding, TTL, atomic helpers
   |-- SearchIndex -------------> FTS5 virtual tables and triggers
   `-- Database ----------------> raw SQL and maintenance
                    |
                    v
          connection + transaction routing
                    |
                    v
                  SQLite
```

There is deliberately no separate asynchronous database implementation.
Synchronous methods contain the behavior. Async methods use
`asyncio.to_thread()` and preserve `ContextVar` state, which keeps one source
of truth for SQL generation, validation, transactions, and error handling.

## Package Boundaries

`obele.__init__` is the public facade. It re-exports the ORM, query,
relationship, signal, pagination, search, exception, and KV symbols so most
applications can use `from obele import ...`.

`obele.orm.__init__` performs the same job for the ORM subpackage.
`obele.kv.__init__` exports `KVStore` and the singleton `KV` convenience
class. Neither initializer implements business logic; they define the public
import surface through `__all__`.

`obele._identity` centralizes names shared by the package, database layer,
and CLI. Keeping these values in one module prevents the environment variable,
savepoint prefix, logger name, and command name from drifting apart.

## Database Configuration and Binding

`Database` is a process-wide class rather than an object users instantiate.
Its class state contains the default binding, locks, thread-local connections,
transaction context, and logging configuration.

A binding (`_Binding`) represents one database target. It contains:

- the normalized path and custom pragmas;
- whether the target is in memory;
- a unique key used by thread-local connection maps;
- an anchor connection that keeps a shared in-memory database alive;
- a small pool of reusable transaction connections; and
- every connection owned by that binding, so cleanup is deterministic.

`Database.configure()` replaces the global binding. It first closes the old
binding, increments the connection generation, normalizes `:memory:`, and
stores logging settings. If configuration is omitted, `_binding()` lazily
creates a binding using `OBELE_DATABASE`, falling back to `DB.sqlite3`.

`Database.using()` creates a `DatabaseScope`. Entering it installs a temporary
binding in the `_scoped` `ContextVar`; leaving restores the previous token and
closes all scoped connections. Since context variables propagate through
`asyncio.to_thread()`, the same scope works with `with` and `async with`.

### Connection selection

Every operation eventually calls `Database._connection()`. Selection follows
this priority:

1. If `_txn` contains a transaction state, use its pinned connection.
2. Otherwise, choose the scoped binding when one exists, or the global binding.
3. Reuse the current thread's connection for that binding and configuration
   generation.
4. Create and register a connection if no reusable entry exists.

New file-backed connections enable WAL, memory mapping, normal synchronous
mode, foreign keys, a busy timeout, and the default cache size. Custom pragmas
are then applied. Every connection also registers the deterministic `REGEXP`
function used by the query lookup system.

A literal `:memory:` path would ordinarily create an isolated database per
connection. `_normalize_path()` changes it to a named shared-cache URI, and
the binding holds an anchor connection open. Consequently, thread-local and
transaction connections all see the same in-memory data.

## Reads, Writes, and Async Work

Reads use `execute_read()` and do not acquire the process write lock. Under
WAL, file-backed connections can read concurrently. `fetchone()`, `fetchall()`,
and `fetch_value()` are small materialization helpers over that cursor.

Writes use `execute()`. Outside a transaction, the process-wide `_write_lock`
serializes synchronous writes. Inside a transaction, `execute()` detects the
pinned `_TxnState` and uses its connection directly because transaction entry
already owns the relevant serialization mechanism.

`executemany()` is atomic even without an explicit transaction: it opens an
`IMMEDIATE` transaction, runs all parameter sets, commits on success, and
rolls back on a SQLite error. `execute_script()` is disallowed inside an Obele
transaction because SQLite's script execution may commit implicitly.

The async helpers divide operations into two categories:

- `athread()` runs blocking reads in a worker thread.
- `awrite()` first enters the event-loop-specific async write gate, then runs
  the synchronous write in a worker thread.

When already inside an async transaction, `_async_gate()` returns `_NullGate`
because the transaction owns the real gate. Async reads retain parallelism;
async writes and async transactions are serialized per event loop.

`aexecute()` cannot safely return a live worker-thread cursor. It therefore
materializes rows, `rowcount`, and `lastrowid` into the immutable `ExecResult`.
`ExecResult` is iterable, has a length, and exposes its first row through the
`first` property.

## Transaction Lifecycle

`Database.transaction(mode)` returns one object implementing both synchronous
and asynchronous context-manager protocols. Valid modes are `DEFERRED`,
`IMMEDIATE`, and `EXCLUSIVE`.

For a root synchronous transaction:

1. `Transaction.__enter__()` acquires `_write_lock`.
2. `_begin_root()` checks out a dedicated connection and issues `BEGIN`.
3. A `_TxnState` holding that connection is installed in the `_txn`
   `ContextVar`.
4. Every nested database/model/KV call routes to that connection.
5. Normal exit commits; exceptional exit rolls back.
6. The context token is reset, the connection is returned to the small
   transaction pool, and the lock is released.

An async root transaction follows the same lifecycle in worker threads but
uses the event loop's async lock instead of blocking `_write_lock` on the loop.

If `_txn` already has state, the transaction is nested. Obele increments a
counter and creates a savepoint named with `SAVEPOINT_PREFIX`. A successful
nested exit releases it; a failed nested exit rolls back to it and then
releases it. The root transaction can continue after a handled nested failure.

`_wrap_errors` maps `sqlite3.IntegrityError` to Obele's `IntegrityError` and
other `sqlite3.Error` instances to `DatabaseError`, preserving the original
exception as the cause.

## From a Model Class to a Table

Fields are Python descriptors. Declaring `name = TextField()` creates an
object that controls assignment, conversion, validation, and DDL for that
attribute. Python invokes `Field.__set_name__()` during class creation; it
records the attribute name, derives or validates the column name, and clears
any cached DDL.

`MetaModel.__new__()` runs when a `Model` subclass is defined. It:

1. collects fields inherited from model bases and mixins;
2. adds fields declared in the new class;
3. chooses `table_name` (lowercase class name by default) and validates it;
4. injects an integer `id` primary key when no primary key was declared;
5. records primary-key metadata and whether SQLite can generate it;
6. precomputes hydration and insert plans;
7. copies table-level constraints; and
8. registers the model and attempts to install reverse relationships.

The precomputed plans avoid rebuilding common structures per row or per
insert. `_hydration` records each attribute, database column, conversion
function, and optional foreign-key cache key. `_insert_sql` and
`_insert_sql_with_pk` cover generated and explicit primary keys.

`create_table()` joins every field's `column_ddl()`, table-level unique and
check constraints, and then creates declared single/composite indexes.
`create_all()` topologically sorts models so referenced tables precede tables
with foreign keys. `drop_all()` reverses that order. Cycles fall back to model
declaration order.

## Field Conversion and Validation

The base `Field` separates three concerns:

- `to_python()` converts assignment or database values to their Python form.
- `validate()` checks nullability, type, choices, and custom validators before
  persistence.
- `to_db()` converts the Python form into a SQLite-compatible value.

Descriptor assignment calls `to_python()` immediately, while full validation
runs from `Model.save()` and bulk helpers. This permits convenient coercion
without skipping write-time constraints.

Defaults are Python-side values or zero-argument callables applied by
`Model.__init__()`. `db_default` is a raw DDL default expression. Constant
Python defaults can also be rendered into DDL by `_sql_literal()`. Callables
are never embedded in DDL because they must execute in Python.

The concrete fields provide these mappings:

| Field | SQLite storage | Python value | Special behavior |
|---|---|---|---|
| `IntegerField` | `INTEGER` | `int` | Optional min/max bounds |
| `TextField` | `TEXT` | `str` | Optional maximum length |
| `RealField` | `REAL` | `float` | Numeric coercion and bounds |
| `BlobField` | `BLOB` | `bytes` | Direct byte storage |
| `DecimalField` | `TEXT` | `Decimal` | Exact string round trip |
| `UUIDField` | `TEXT` | `UUID` | Canonical UUID string |
| `BooleanField` | `INTEGER` | `bool` | Stored as zero or one |
| `DateTimeField` | `TEXT` | `datetime` | ISO-8601, no implicit default |
| `DateField` | `TEXT` | `date` | ISO-8601 date |
| `TimeField` | `TEXT` | `time` | ISO-8601 time |
| `TimestampField` | `INTEGER` | UTC `datetime` | Unix epoch seconds |
| `JSONField` | `TEXT` | Any JSON value | Compact JSON serialization |
| `ForeignKeyField` | `INTEGER` | PK or related model | Lazy relation resolution/cache |
| `EnumField` | `TEXT` | Enum member | Stores the member's value |
| `SlugField` | `TEXT` | `str` | Lowercase slug validation |
| `EmailField` | `TEXT` | `str` | Email-shape validation |
| `PickleField` | `BLOB` | Any object | Python pickle serialization |
| `IPAddressField` | `TEXT` | IPv4/IPv6 object | Canonical address string |

All fields can use `choices` and additional validators. `check` is emitted as
SQLite DDL and is therefore enforced by the database as well.

## Model Instance Lifecycle

Construction applies provided values through descriptors, then Python
defaults, then a `None` placeholder for an omitted primary key. Internal state
tracks whether the object is persisted, annotation values, and a snapshot of
non-primary-key values.

`save()` performs this sequence:

1. validate all fields;
2. determine whether the operation creates or updates;
3. emit `pre_save` and, for an insert, `pre_create`;
4. insert all relevant fields or update only `dirty_fields`;
5. store SQLite's `lastrowid` for generated integer keys;
6. refresh the dirty-tracking snapshot;
7. emit `post_save` and, for an insert, `post_create`.

The `pk` property is independent of the actual primary-key attribute name.
Persisted instances compare by model type and primary key. Unsaved instances
compare by identity. `refresh()` reloads all known fields and invalidates
hydrated foreign-key caches. `delete()` emits delete signals, removes the row,
clears the primary key, and marks the object unpersisted.

`create`, `get_or_create`, `update_or_create`, `get_or_none`, `get_by_pk`, and
`upsert` combine construction and query operations. Race-sensitive compound
operations use transactions. Bulk creation groups dictionaries by column
signature, validates them, respects SQLite's parameter limit through chunks,
uses `INSERT ... RETURNING`, and restores input order. Bulk update applies a
common field set to saved instances in one transaction.

`migrate()` is a schema-sync rebuild, not a migration-history system. It
renames the old table, creates the current model schema, copies compatible or
renamed columns, fills safe Python defaults, drops the old table, and rebuilds
indexes inside one transaction. A new non-nullable field without a usable
default raises `MigrationError` before destructive work begins.

## QuerySet State and Laziness

A `QuerySet` is a mutable implementation detail wrapped in an immutable-style
public API. Chaining methods call `_clone()`, mutate the clone, and return it.
The original query remains reusable.

State includes where/having fragments and parameters, ordering, limit and
offset, joins, select columns, eager-loading instructions, annotations,
grouping, projections, deferred fields, prefetch requests, and optional raw
SQL produced by a set operation.

Model classes expose common query methods through `MetaModel.__getattr__()`.
For example, `User.filter(...)` creates `User._queryset()` and calls its
`filter()`. This indirection is important: `SoftDeleteMixin` overrides
`_queryset()` to add its default `is_deleted=False` condition.

Evaluation happens only at a terminal operation: iteration, indexing,
`all`, `first`, `get`, `count`, `exists`, aggregation, pagination, update, or
delete. Slices adjust offset and limit lazily; integer indexing evaluates a
one-row slice.

## Conditions, Lookups, and Expressions

`Q` stores a small boolean tree. Keyword pairs are leaf conditions; `&` and
`|` create parent nodes; `~` clones and negates a node. `_compile_q()` walks
the tree recursively, grouping each child and accumulating bound parameters.

Lookup names are split from the last `__` component. Earlier components form
a relationship path. Exact, inequality, comparison, LIKE/GLOB, membership,
null, range, case-insensitive, and regex lookups compile into parameterized
SQL. Empty `IN` becomes false and empty `NOT IN` becomes true, avoiding
invalid SQL. LIKE metacharacters are escaped before wildcard patterns are
added.

Expressions implement `as_sql(queryset) -> (sql, params)`:

- `Value` binds a literal.
- `F` resolves a field or joined field.
- `RawSQL` inserts explicitly supplied SQL and parameters.
- `Func` compiles SQL function calls.
- `Count`, `Sum`, `Avg`, `Min`, and `Max` are aggregate `Func` variants.
- `Subquery` compiles another queryset as a scalar/select expression.
- `CombinedExpression` joins two coerced expressions with arithmetic.

Arithmetic operators on `Expression` construct nested
`CombinedExpression` objects. Plain strings matching fields become `F`
references; other values become `Value`. This allows atomic updates such as
`views=F("views") + 1`.

## Joins and Relationships

A `ForeignKeyField` can point directly to a model or name one as a string.
String references resolve lazily through `_model_registry`, which supports
models declared in either order.

Assigning a saved related instance stores its primary key and caches the
instance. Assigning a raw key clears the cache. Reading returns the cached
instance when present or the raw key otherwise; Obele does not automatically
issue a hidden query on attribute access.

Once both sides are registered, `_register_reverse_relations()` installs a
`ReverseRelationDescriptor` on the target model. Accessing it through an
instance returns a `ReverseRelationManager`, which builds a queryset filtered
by the owner's key and delegates the remaining query API. `create()` also
injects that foreign key.

`_ensure_join()` creates and caches `_JoinSpec` records. Forward joins compare
the source foreign-key column with the target primary key. Reverse joins
compare the related model's foreign-key column with the source primary key.
Aliases (`jt0`, `jt1`, ...) prevent collisions in nested paths.

`select_related()` adds a left join and aliases every related column. Row
conversion hydrates the related model and places it in the foreign-key cache.
`prefetch_related()` instead runs one extra query per reverse relation, groups
children by foreign key, and stores lists on each parent. The reverse manager
uses that cache for `all`, `count`, and iteration.

## SQL Construction and Row Conversion

`_build_select()` composes SQL in this order: select list, source table,
joins, where, automatic or explicit grouping, having, ordering, limit, and
offset. Parameters are appended in the same order as their placeholders.

Aggregate annotations automatically group by the model primary key unless an
explicit group is supplied. `values()` and `values_list()` change the row
converter; `only()` and `defer()` change selected model columns. Set operations
store the complete left/right SQL in `_raw_sql`; only ordering, limit, and
offset remain composable afterward.

Model rows pass through `Model._from_row()`. It bypasses normal construction,
uses the precomputed hydration plan, marks the object persisted, records a
snapshot, and attaches annotations. Projected rows become dictionaries,
tuples, or flat values instead.

`iterator()` fetches chunks from a live cursor. `aiterator()` creates and
fetches that cursor in worker threads. `all()` materializes and then performs
prefetching when requested.

## Pagination

Offset pagination performs a count query and then applies `OFFSET`/`LIMIT`.
It returns an immutable `Page` containing results, totals, page count, and
navigation flags.

Cursor pagination filters a cursor field (the primary key by default) with
`gt` or `lt`, requests one extra row to detect a next page, and returns a
`CursorPage` with boundary cursor values. It avoids the increasing scan cost
of large offsets, though callers should use stable, unique ordering for
reliable traversal.

## Signals and Mixins

`Signal` is a thread-safe publisher. Receivers are grouped by `id(sender)` or
under `None` for global listeners. Connections can be strong or weak.
`send()` copies the applicable receiver lists under a lock, releases the lock,
then invokes callbacks and returns their results. The `receiver()` decorator
connects one function to one or several signals.

`TimestampMixin.save()` sets `created_at` only on first persistence and always
sets `updated_at`, then follows normal method resolution to `Model.save()`.

`SoftDeleteMixin.delete()` updates deletion fields and saves instead of
issuing SQL DELETE. Its `_queryset()` override scopes ordinary model queries.
`with_deleted()` and `only_deleted()` explicitly construct unscoped querysets;
`hard_delete()` calls `Model.delete()` directly.

Because `Model.asave()` and `Model.adelete()` execute `self.save` and
`self.delete`, normal dynamic dispatch reaches mixin overrides. The mixins do
not need duplicate async save/delete implementations.

## Full-Text Search

`SearchIndex` validates the source fields and derives an FTS table name. With
content synchronization enabled, `create()` builds an external-content FTS5
table plus insert, update, and delete triggers. The triggers mirror changes
made through the ORM or through raw SQL.

`rebuild()` asks FTS5 to repopulate from the source table; `optimize()` asks it
to merge internal index segments. `search()` joins FTS row IDs back to the
model primary key and orders results by FTS rank before hydrating models.
`search_count()` avoids hydration and returns only the match count.

## Key-Value Storage

`KVStore` uses the same `Database` layer but owns a purpose-built `WITHOUT
ROWID` table. One physical table can contain multiple namespaces. The primary
key, `lookup_key`, is a type-tagged binary identity, optionally prefixed by the
namespace. Separate typed columns (`key_int`, `key_real`, `key_text`, and
`key_blob`) preserve native ordering and are covered by partial indexes.

With `enforce_key_type=True`, the first key fixes the store's sortable type
unless `key_type` declared it earlier. Existing rows are inspected at startup.
Mixed or arbitrary hashable keys are possible when enforcement is disabled;
unrecognized key types use pickle, but ordered range operations are then
unavailable.

Values use JSON, pickle, or custom callables. `auto` first attempts a lossless
JSON round trip; it falls back to pickle if JSON would change the type or
value. The stored `value_format` ensures decoding chooses the matching path.

TTL is stored as an absolute Unix timestamp. Reads exclude expired rows and a
direct lookup lazily deletes an expired match. `purge_expired()` performs
eager cleanup. Namespaces are included in every query, so `clear`, counts,
ranges, and TTL operations remain isolated.

Dictionary operations map onto single statements or transactions. `pop`,
`popitem`, and `setdefault` are transactional read-modify-write operations.
`increment` starts missing keys at zero; `compare_and_swap` changes a value
only when the current value matches. Batch methods use one query or atomic
`executemany()`.

Ranges operate on the native typed key column with inclusive start and
exclusive stop. Prefix operations compute the smallest upper string bound,
allowing indexed range scans instead of a leading-wildcard LIKE. `scan()` uses
SQLite GLOB for explicit patterns.

`memoize()` builds string keys from the function's qualified name, positional
arguments, and sorted keyword arguments. It selects a sync or coroutine
wrapper by inspecting the decorated function. Cached values use the store's
normal serializer and optional TTL.

`KV` subclasses `KVStore` and implements a process-wide singleton with
double-checked locking. Only the first constructor arguments take effect.
`KV.reset()` discards the singleton reference so a later call can initialize a
different store.

## CLI Migration Flow

The `obele-orm` command is registered by `pyproject.toml` and calls
`obele.orm.cli.main`. `python -m obele.orm` reaches the same function.

`list-models` imports requested modules or explicit model paths, removes
duplicates, topologically orders them, and prints qualified names and tables.

`migrate` additionally parses pragmas and rename specifications, configures
the database, and invokes each model's `migrate()` in dependency order. CLI
scalar parsing recognizes booleans, null, integers, and floats before falling
back to strings. Errors become normal `argparse` usage errors, and the current
thread's connection is closed in a `finally` block.

## Error Boundaries and Safety

Obele's exception hierarchy lets callers distinguish validation, query
cardinality, database, integrity, migration, and configuration failures while
still catching `ORMError` for all library-level errors.

Generated identifiers cannot use SQL parameters, so table, column, pragma,
annotation, and FTS names pass through `validate_identifier()`. Data values
use bound parameters. Raw SQL surfaces (`RawSQL`, field `check`, `db_default`,
tokenizer specifications, and direct database calls) intentionally trust the
developer and should not receive untrusted input.

Pickle-backed fields, KV values, and flexible KV keys must likewise only read
data from trusted databases because unpickling can execute code.

## End-to-End Example

For `await User.filter(age__gte=18).order_by("name").aall()`:

1. `MetaModel.__getattr__` creates a fresh `QuerySet(User)` and resolves
   `filter`.
2. `filter()` clones the queryset, builds a `Q`, resolves `age`, calls the
   field's `to_db()`, and stores `users.age >= ?` with parameter `18`.
3. `order_by()` clones again and stores `users.name ASC`.
4. `aall()` sends `all()` to a worker thread through `athread()`.
5. `all()` calls `iterator()`, which builds parameterized SELECT SQL.
6. `Database.execute_read()` selects the transaction connection when inside a
   transaction, otherwise the worker thread's connection.
7. Each `sqlite3.Row` passes through `_row_to_instance()` and
   `User._from_row()`; every field converts its column to Python.
8. The resulting list crosses back to the event loop with no live cursor or
   connection attached.

That same routing path is used by model helpers, relationships, pagination,
search, and KV storage, which is why scoped databases and transactions remain
consistent across the whole library.
