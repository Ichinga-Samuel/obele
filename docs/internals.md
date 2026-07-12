# Complete Source Reference

This is a source-level map of the library. It covers every Python module,
module constant, state variable, helper, class, and method. Names beginning
with `_` are implementation details and may change without a compatibility
guarantee. For the design rationale and end-to-end flows, read
[Architecture and Execution Model](architecture.md).

## `obele._identity`

This module prevents runtime names from being repeated as string literals.

| Symbol | Role |
|---|---|
| `PACKAGE_NAME` | The canonical package and logger name, `"obele"`. |
| `CLI_PROGRAM` | The console program name, derived as `"obele-orm"`. |
| `DATABASE_ENV_VAR` | The configuration override name, `OBELE_DATABASE`. |
| `SAVEPOINT_PREFIX` | Prefix for generated nested-transaction savepoints. |
| `default_database_path()` | Returns the environment override or `DB.sqlite3`. It reads the environment on every call rather than at import time. |

## Package Initializers

`obele.__init__` imports the ORM and KV public symbols into one namespace. It
sets `__title__` from `PACKAGE_NAME`; `__all__` is the authoritative star-import
surface. `obele.orm.__init__` and `obele.kv.__init__` perform equivalent
re-export work for their subpackages.

`obele.orm.__main__` calls `cli.main()` and raises `SystemExit` with its return
code, enabling `python -m obele.orm`.

## `obele.orm.sql`

`_IDENTIFIER_RE` accepts a letter or underscore followed by letters, digits,
or underscores. `validate_identifier(name, kind=...)` applies that expression
and returns the unchanged name on success. It raises `ValueError` otherwise.
This helper protects generated identifier positions, which cannot be protected
with SQL parameter placeholders.

## `obele.orm.exceptions`

All exception classes are intentionally behavior-free; their inheritance
communicates failure categories.

| Exception | Meaning |
|---|---|
| `ORMError` | Base for Obele-specific failures. |
| `FieldValidationError` | Conversion, type, nullability, choice, bound, or validator failure. |
| `RecordNotFoundError` | An operation required one row but found none. |
| `MultipleResultsError` | `get()` found more than one row. |
| `DatabaseError` | ORM wrapper for a general `sqlite3.Error`. |
| `IntegrityError` | More specific database wrapper for constraint failures. |
| `MigrationError` | A model schema cannot safely be synchronized. |
| `ConfigurationError` | Reserved configuration-error category. |

## `obele.orm.database`

### Module state and helpers

| Symbol | Role |
|---|---|
| `logger` | Package logger used for query and slow-query output. |
| `_mem_seq` | Monotonic counter making each normalized in-memory database URI unique. |
| `_binding_seq` | Monotonic counter assigning each binding a thread-local map key. |
| `TransactionMode` | Type alias documenting the accepted transaction-mode strings. Runtime validation occurs in `Transaction`. |
| `_compile_regex()` | LRU-cached (`256` entries) regular-expression compiler. |
| `_regexp()` | SQLite callback implementing `REGEXP`; propagates SQL null as `None`. |
| `_normalize_path()` | Converts bare `:memory:` to a named shared-cache URI and reports whether the result is memory-backed. |
| `athread()` | Runs a blocking callable with `asyncio.to_thread`; used primarily for reads. |
| `awrite()` | Enters the async write gate and then runs a blocking callable in a worker. |
| `_NULL_GATE` | Singleton no-op async context manager used by writes already inside a transaction. |

### `ExecResult`

An immutable, slotted dataclass containing `rows`, `rowcount`, and
`lastrowid` from an asynchronous statement. `__iter__()` iterates materialized
rows, `__len__()` returns their count, and `first` returns the first row or
`None`. It intentionally owns no cursor or connection.

### `_Binding`

Internal slotted dataclass for one database target. `db_path`, `pragmas`, and
`is_memory` describe the target. `key` uniquely identifies it. `anchor` keeps
shared memory alive, `txn_pool` caches up to two transaction connections, and
`owned` tracks all opened connections. `close_all()` best-effort closes every
tracked connection and clears all collections.

### `_TxnState`

Internal context state with the pinned `conn` and a monotonically increasing
`savepoints` counter. One instance is shared by all nested transactions in the
same context.

### `_NullGate`

`__aenter__()` and `__aexit__()` do nothing. It presents the same async
context-manager interface as `asyncio.Lock` without reacquiring a non-reentrant
lock inside an active async transaction.

### `DatabaseScope`

`__init__()` normalizes a path and creates a private `_Binding`. `__enter__()`
sets that binding in `Database._scoped` and returns `Database`. `__exit__()`
resets the exact context token and closes scoped resources under the global
lock. The async entry delegates to sync entry; async exit restores the token
and closes on a worker via `_close_binding()`. Scope state is context-local,
not a process-wide reconfiguration.

### `Transaction`

`__init__()` uppercases and validates the mode and initializes ownership flags,
context tokens, state, and optional savepoint name.

`_begin_nested()` increments the shared counter, creates a named savepoint,
and returns the existing pinned connection. `_begin_root()` checks out a
dedicated connection, issues `BEGIN <mode>`, and creates `_TxnState`; a failed
begin immediately releases the connection.

`_finish()` releases or rolls back a savepoint for nested blocks, or commits or
rolls back a root block. `_cleanup_root()` resets the transaction context and
returns only root connections to the pool.

`__enter__()` reuses the current transaction for nesting; otherwise it owns
the synchronous write lock, begins a root transaction, and sets `_txn`.
`__exit__()` finishes, cleans up, and always releases its lock. `__aenter__()`
and `__aexit__()` mirror that lifecycle with an event-loop lock and worker
threads so the loop is never blocked by SQLite.

### `_wrap_errors`

A private context manager. `__enter__()` has no value. `__exit__()` returns
normally when no error occurred, maps SQLite integrity errors to
`IntegrityError`, maps other SQLite errors to `DatabaseError`, and lets
non-SQLite exceptions propagate unchanged.

### `Database` class state

| Attribute | Role |
|---|---|
| `_lock` | Reentrant guard for binding bookkeeping and connection ownership. |
| `_write_lock` | Serializes root sync writes and sync transactions. |
| `_local` | Per-thread connection dictionary. |
| `_generation` | Invalidates thread-local entries after global cleanup/reconfiguration. |
| `_global_binding` | Default database target, created lazily if needed. |
| `_log_queries` | Enables debug SQL logging. |
| `_slow_query_threshold` | Positive warning threshold in seconds. |
| `_scoped` | Context-local database override. |
| `_txn` | Context-local pinned transaction state. |
| `_async_lock` / `_async_lock_loop` | Async writer lock and the event loop that owns it. |

### `Database` configuration and routing methods

| Method | Behavior |
|---|---|
| `__repr__()` | Shows the active path and whether the current context is scoped. |
| `configure()` | Closes the previous global binding, creates a new binding, and stores query logging settings. |
| `aconfigure()` | Worker-thread delegate of `configure()`. |
| `using()` | Constructs a `DatabaseScope`; does not enter it. |
| `transaction()` | Constructs a `Transaction`; does not begin it. |
| `current_config()` | Returns a copy of the active path and pragma dictionary. |
| `_binding()` | Selects scoped/global state and lazily initializes the default binding. |
| `_new_connection()` | Opens/configures SQLite, registers `REGEXP`, creates a memory anchor if needed, and records ownership. |
| `_connection()` | Chooses transaction connection first, then the generation-valid thread-local connection for the active binding. |
| `_checkout_txn_conn()` | Pops a pooled transaction connection or opens one. |
| `_release_txn_conn()` | Retains at most two transaction connections; closes extras. |
| `_get_async_lock()` | Creates a lock for the current event loop and replaces a lock belonging to a different loop. |
| `_async_gate()` | Returns no-op gate inside a transaction, otherwise the loop's write lock. |
| `close()` | Closes and unregisters connections owned by the current thread. |
| `close_all()` | Invalidates and closes every connection for the global binding. |
| `aclose_all()` | Worker-thread delegate of `close_all()`. |
| `status()` | Returns path, memory flag, open/pooled connection counts, and scope status. |

### `Database` execution methods

| Method | Behavior |
|---|---|
| `_log_query()` | Computes elapsed time, emits debug SQL when enabled, and warns above the slow threshold. |
| `execute()` | Runs one write on the pinned transaction or under `_write_lock`; returns `sqlite3.Cursor`. |
| `executemany()` | Runs many parameter sets; outside a transaction wraps them in an atomic `BEGIN IMMEDIATE`. |
| `execute_script()` | Runs a semicolon-separated script outside transactions only. |
| `execute_read()` | Runs SQL without the process write lock and returns the cursor. |
| `fetchone()` | Executes a read and fetches one row. |
| `fetchall()` | Executes a read and fetches all rows. |
| `fetch_value()` | Returns one indexed/named column from the first row, or `None`. |
| `_execute_result()` | Executes a write and consumes any returned rows into `ExecResult`. |
| `_executemany_result()` | Adapts `executemany()` metadata to an empty-row `ExecResult`. |
| `aexecute()` | Async gated write returning `ExecResult`. |
| `aexecutemany()` | Async gated batch write returning `ExecResult`. |
| `aexecute_script()` | Async gated script execution. |
| `afetchone()` / `afetchall()` / `afetch_value()` | Worker-thread read delegates. |

### `Database` maintenance methods

`pragma()` validates the pragma name; reads when `value` is `None`, otherwise
accepts numeric or conservative alphanumeric punctuation values and writes the
pragma. `apragma()` is its async delegate.

`optimize()` runs `PRAGMA optimize`; `vacuum()` runs `VACUUM` outside a
transaction under the write lock; `integrity_check()` returns SQLite's string
verdict; `tables()` lists non-internal tables; and `backup()` copies the active
connection into a separately opened target connection. Each has an `a`-prefixed
worker-thread equivalent: `aoptimize`, `avacuum`, `aintegrity_check`, `atables`,
and `abackup`.

## `obele.orm.fields`

### Module constants and helpers

`_MISSING` distinguishes an omitted default from an explicit `None`.
`_ON_DELETE_ACTIONS` is the accepted SQLite foreign-key action set.
`_SLUG_RE` and `_EMAIL_RE` drive specialized text validation. `_sql_literal()`
renders null, booleans, numbers, blobs, and escaped text for safe constant DDL
defaults.

### `Field`

Class attributes `sql_type` and `python_type` define the default mapping.
Instances store primary-key, nullability, Python/database defaults, uniqueness,
indexing, column/attribute names, validators, check expression, choices, and a
cached DDL string.

| Method | Behavior |
|---|---|
| `__init__()` | Stores declarative options; turns validators/choices into immutable tuples. |
| `__set_name__()` | Binds the descriptor to an attribute, derives and validates its column name, clears DDL cache. |
| `__get__()` | Returns the descriptor on the class or stored value on an instance. |
| `__set__()` | Enforces nullability and stores `to_python(value)`. |
| `_reject_null()` | Raises unless the field is nullable or is a primary key. |
| `to_python()` | Returns already-correct values or calls `python_type(value)`, wrapping conversion failures. |
| `to_db()` | Identity conversion in the base class. |
| `validate()` | Enforces nullability, exact Python type, choices, and validators. |
| `_run_extra_validation()` | Checks choices and invokes custom validator callables. |
| `column_ddl()` | Lazily builds and caches the column definition and constraints. |
| `__repr__()` | Shows concrete field class and bound column. |

`_BoundedNumericMixin._init_bounds()` stores `min_value` and `max_value`;
`_check_bounds()` enforces inclusive limits.

### Concrete fields

`IntegerField.__init__()` initializes bounds; `validate()` adds their checks.
`TextField.__init__()` stores `max_length`; `validate()` checks it.
`RealField.__init__()` stores bounds, `to_python()` accepts integers/floats,
and `validate()` accepts both numeric types, applies bounds, and calls custom
validators with a float. `BlobField` needs no overrides.

`DecimalField.to_python()` uses `Decimal(str(value))`; `to_db()` stores text;
`validate()` validates the converted decimal. `UUIDField` follows the same
pattern with `uuid.UUID` and canonical strings.

`BooleanField.to_python()` recognizes string `1/true/yes` and otherwise uses
truthiness; `to_db()` stores zero/one. `DateTimeField`, `DateField`, and
`TimeField` parse/emit ISO-8601. `TimestampField.to_python()` accepts datetime,
epoch numeric, or ISO text; `to_db()` emits integer epoch seconds; `validate()`
accepts datetime or numeric input.

`JSONField.to_python()` parses stored strings, `to_db()` emits compact Unicode
JSON, and `validate()` allows any non-null JSON candidate plus extra
validation; serializability is checked during `to_db()`.

`ForeignKeyField.__init__()` validates `on_delete` and stores direct/string
target plus optional reverse name. `cache_attr_name` derives its instance cache
key. `__get__()` prefers the related-object cache; `__set__()` accepts `None`,
a saved target object, or a raw key and maintains that cache. `_instance_pk()`
rejects unsaved targets. `related_model` lazily resolves string references.
`column_ddl()` extends base DDL with `REFERENCES` and `ON DELETE`. `validate()`
accepts instances or key values; `to_db()` extracts an instance key.

`EnumField.__init__()` stores the enum class. `to_python()` tries existing
member, raw value, string value, member name, and integer-like value.
`to_db()` stores `.value`; `validate()` confirms conversion and runs validators;
`__set__()` stores the enum member.

`SlugField` defaults to length 255 and adds `_SLUG_RE` validation.
`EmailField` defaults to length 254 and adds `_EMAIL_RE` validation.
`PickleField.to_python()` unpickles byte-like values, `to_db()` pickles with
the highest protocol, and `validate()` allows arbitrary non-null values.
`IPAddressField.to_python()` calls `ip_address`, `to_db()` stores canonical
text, and `validate()` checks conversion plus extra validators.

## `obele.orm.model`

### Registry and query proxy

`_model_registry` maps class names to model classes for string foreign keys.
`_QUERYSET_PROXY` is the fixed set of query methods exposed dynamically on
model classes. Write methods such as queryset `update()` and `delete()` are
not proxied; callers begin from a queryset such as `User.filter(...)`.

### Reverse relationships

`ReverseRelationManager.__init__()` binds owner, related model, FK attribute,
and accessor name. `__repr__()` displays those identities. `_prefetched`
reads the cache; `_queryset()` rejects unsaved owners and creates an FK-filtered
queryset. `__getattr__()` delegates the rest of the queryset API.

`all`/`aall`, `count`/`acount`, and sync/async iteration use prefetched data
when present. `create`/`acreate` inject the owner into the related foreign key.

`ReverseRelationDescriptor.__init__()` stores relation metadata;
`__get__()` returns itself through a class and a bound manager through an
instance. `_register_reverse_relations()` scans all registered foreign keys,
resolves available targets, chooses `related_name` or `<source>_set`, prevents
attribute collisions, installs descriptors, and records them.

### `MetaModel`

`__new__()` collects descriptors, assigns/validates the table, creates the
class, injects a default integer primary key for concrete models, records PK
metadata, builds plans and constraints, registers the class, and refreshes
reverse relations. `__getattr__()` creates a fresh queryset for approved proxy
methods. `_build_plans()` precomputes hydration tuples, field-name tuples,
RETURNING columns, and insert SQL with/without an explicit PK. Its local
`insert_sql()` helper renders the column list and matching parameter markers
for either precomputed insert variant.

### `Model` declared class state

`table_name`, `unique_together`, `index_together`, and `check_constraints` are
user configuration. `_fields`, primary-key metadata, reverse relations,
hydration/insert plans, and copied constraint lists are metaclass-generated.

### `Model` identity and state methods

`__init__()` applies inputs/defaults and initializes `_persisted`,
`_annotations`, and `_snapshot`. `pk` reads/writes the actual PK attribute.
`_take_snapshot()` records non-PK values. `dirty_fields` returns all non-PK
values for new objects or only changes for persisted objects; `is_dirty` is
its boolean form. `__repr__()` shows class and key. `__eq__()` compares saved
objects by same class/key and unsaved objects by identity. `__hash__()` hashes
class name and key.

### `Model` schema and migration methods

`_create_table_sql()` combines field DDL, composite unique groups, and check
constraints. `_create_index_sqls()` builds single and composite index SQL.
`create_table`/`acreate_table` and `drop_table`/`adrop_table` execute DDL.

`_table_exists()` queries `sqlite_master`. `_migration_default_value()` returns
a validated DB value, `_MISSING` when SQLite/default/nullability can handle the
column, or raises for unsafe new required columns. `migrate()` performs the
transactional table rebuild; `amigrate()` runs it through `awrite()`.

### `Model` persistence and serialization methods

`_validate_all()` validates every current field value. `save()` coordinates
validation, lifecycle signals, insert/update, and snapshots; `asave()` is its
async write delegate. `_insert()` selects a prebuilt insert plan, converts
values, and captures `lastrowid`. `_update()` sends only dirty fields and uses
the configured PK column.

`delete`/`adelete` remove one saved instance and emit signals. `refresh` and
`arefresh` reload fields, clear FK caches, and reset the snapshot.

`to_dict(mode="python"|"db")` serializes fields plus optional annotations;
`to_db_dict()` selects DB mode. `_from_row()` bypasses `__init__`, hydrates via
the precomputed plan, marks persistence, attaches annotations as both metadata
and attributes, and creates the initial snapshot.

### `Model` creation and query helpers

`create`/`acreate` instantiate and save. `get_or_create`/`aget_or_create` and
`update_or_create`/`aupdate_or_create` perform transactional fetch-or-write
flows and return `(instance, created)`. `get_or_none`/`aget_or_none` suppress
only `RecordNotFoundError`. `get_by_pk`/`aget_by_pk` use direct SQL unless a
model overrides `_queryset()` (for example soft delete), in which case they
honor that scope.

`_build_upsert()` validates input, chooses/validates conflict and update
fields, converts values, and builds `ON CONFLICT ... RETURNING`. `upsert()`
runs it transactionally and fetches the surviving row after `DO NOTHING`;
`aupsert()` delegates asynchronously.

`_bulk_rows()` applies defaults, optional validation, DB conversion, and groups
input rows by column signature while collecting indexed validation errors.
`bulk_create`/`abulk_create` issue chunked multi-row inserts with RETURNING and
restore input order. `bulk_update`/`abulk_update` update a chosen/common set of
fields for saved instances and refresh snapshots.

`raw`/`araw` execute caller-provided SELECT SQL and hydrate model rows.
`_queryset()` returns a fresh `QuerySet` and is the extension point for default
scopes.

### Schema helper functions

`_toposort_models()` computes FK dependencies, uses stable input order among
ready models, and appends cyclic leftovers. `registered_models()` returns the
registry in that order. `create_all()` and `acreate_all()` create targets in
dependency order. `drop_all()` and `adrop_all()` drop them in reverse order.

## `obele.orm.query`

### Lookup constants

`_LOOKUPS` maps supported suffixes to SQL forms or internal handler labels.
`_LIKE_ESCAPE_CHAR` is backslash. `_LIKE_PATTERNS` defines wildcard placement
for contains/start/end and case-insensitive variants. `_escape_like()` escapes
backslash, percent, and underscore in user values. `_LOOKUP_DISPATCH` maps
membership, null, and range lookups to specialized compilers.

### `Q`

`__init__()` stores child `Q` objects and lookup pairs with `AND` as default.
`_combine()` creates a two-child parent. `__and__()` and `__or__()` use it;
`__invert__()` clones and toggles negation without modifying the original.

### Expressions

`Expression.as_sql()` is abstract. `_combine()` constructs a
`CombinedExpression`; forward/reverse addition, subtraction, multiplication,
division, and modulo methods call it (`__add__`, `__radd__`, `__sub__`,
`__rsub__`, `__mul__`, `__rmul__`, `__truediv__`, `__rtruediv__`, `__mod__`).

`CombinedExpression.__init__()` stores operands/operator; `as_sql()` coerces
both sides, parenthesizes the operation, and concatenates parameters.
`Value` stores one literal and compiles to `?`. `F` stores a field path and
resolves it. `RawSQL` stores explicit SQL/parameters and returns copies.

`Func` stores a function name, arguments, and aggregate flag; `as_sql()`
coerces every argument. `Count`, `Sum`, `Avg`, `Min`, and `Max` select names
and mark aggregates. Empty `Count()` substitutes `*`. `Subquery` stores a
queryset and optional field and compiles a one-column select. `_STAR` is the
shared `RawSQL("*")` expression.

`_JoinSpec` is a slotted dataclass storing path, SQL alias, related model,
relation metadata, and the complete JOIN fragment.

### `QuerySet` initialization and protocol

`__init__()` creates empty state for filters, ordering, limits, joins,
selections, annotations, grouping, values modes, field loading, prefetch, and
set-operation SQL. `__iter__()` streams unless prefetch requires full
materialization. `__aiter__()` mirrors that behavior. `__len__()` calls
`count`; `__bool__()` calls `exists`; `__getitem__()` implements non-negative
indexing and step-free lazy slices. `__repr__()` shows compiled SQL/params.
`_clone()` copies every mutable container. `_assert_composable()` blocks
operations that cannot be applied after a set operation.

### `QuerySet` chain methods

`filter()` and `exclude()` accept `Q` objects plus keyword lookups and append
compiled where fragments. `order_by()` resolves fields/annotations and stores
directional SQL. `limit()` and `offset()` reject negatives.

`distinct()` sets DISTINCT. `values()` selects dictionary projection;
`values_list()` selects tuples or one flat value. `only()` includes selected
model fields plus PK; `defer()` omits selected fields. Both validate names.

`join()` creates an explicit forward/reverse join. `select_related()` supports
direct FKs and adds aliased target columns. `prefetch_related()` de-duplicates
reverse names while preserving order.

`annotate()` validates aliases and coerces expressions. `group_by()` resolves
columns. `having()` compiles conditions into the HAVING collection.

`union()`, `intersection()`, and `difference()` call `_set_operation()` with
`UNION`/`UNION ALL`, `INTERSECT`, or `EXCEPT`. `_set_operation()` compiles both
sides and stores their combined SQL and parameters in a fresh queryset.

### `QuerySet` compiler and conversion methods

`_get_select_columns()` chooses only/defer/values/default columns.
`_build_select()` assembles the query and parameters, adds automatic PK
grouping for aggregate annotations, and has a separate set-operation path.
`_limit_offset_sql()` emits SQLite's `LIMIT -1` when offset exists alone.
`as_sql()` exposes compilation; `explain()` runs `EXPLAIN QUERY PLAN` and
formats details.

`_row_to_instance()` hydrates the base model, annotation values, and any
selected related object. `_row_to_values()` maps database columns/aliases to
flat, tuple, or dictionary projection; its local `value_of()` resolves a model
field through its physical column and falls back to an annotation alias.
`_converter()` selects between the two row conversion paths.

### `QuerySet` terminal methods

`all()` materializes and optionally prefetches; `aall()` delegates. `first()`
uses limit one; `last()` calls `_reversed().first()`; `_reversed()` flips each
direction or defaults to descending PK. Async twins are `afirst` and `alast`.

`get()` filters optionally, limits to two, and enforces exactly one result;
`aget()` delegates. `latest`/`alatest` and `earliest`/`aearliest` order by a
chosen field or PK and raise on empty results. `in_bulk`/`ain_bulk` return a
dictionary keyed by a chosen field, optionally filtering to supplied values.

`iterator()` fetches cursor chunks synchronously. `aiterator()` creates and
fetches the cursor via worker calls. `count`/`acount` wrap the built select;
`exists`/`aexists` wrap it in `SELECT 1 ... LIMIT 1`.

`aggregate()` validates one of five function names, selects the target into a
subquery, and aggregates it; `aaggregate()` delegates. `paginate`/`apaginate`
and `cursor_paginate`/`acursor_paginate` lazily import pagination helpers.

`_do_prefetch()` validates reverse names, fetches all children with one `IN`
query per relation, groups them by FK, and installs parent caches.

`update()` validates/coerces literal or expression values, rejects joined/set
queries, and returns affected rows; `aupdate()` is gated async. `delete()`
builds a direct filtered DELETE with the same restrictions; `adelete()` is its
async twin. Bulk queryset writes do not emit per-instance signals.

### `QuerySet` internal lookup/join methods

`_split_lookup()` separates a recognized final suffix. `_compile_q()` walks
and groups the boolean tree. `_compile_condition()` resolves fields or
annotations, dispatches special lookups, handles null, subquery, and expression
values, and converts literals through fields. `_compile_like()` creates escaped
case-sensitive or lowercase patterns.

`_coerce_expression()` preserves expressions, wraps querysets as subqueries,
turns `*` into `_STAR`, recognizes field/path strings as `F`, and wraps all
else in `Value`. `_resolve_field_reference()` creates joins for path prefixes
and returns qualified column plus field object. `_ensure_join()` recursively
creates/caches forward or reverse `_JoinSpec` objects.

`_compile_in()` supports subqueries, converts iterable values, and maps empty
input to false. `_compile_not_in()` maps empty input to true. `_compile_is_null()`
chooses `IS NULL`/`IS NOT NULL`. `_compile_between()` requires exactly two
bounds and applies field conversion.

## `obele.orm.pagination`

`Page` is an immutable slotted dataclass with `items`, one-based `page`,
`per_page`, `total`, `pages`, `has_next`, and `has_prev`. `CursorPage` contains
`items`, `per_page`, navigation flags, and start/end cursors. Both implement
`__iter__()`, `__len__()`, and a diagnostic `__repr__()`.

`paginate_queryset()` validates positive values, counts, computes at least one
page even for an empty result, applies offset/limit, and returns `Page`.
`cursor_paginate_queryset()` validates size, chooses the given field or PK,
applies after/before comparison, fetches one extra result, trims it, and
returns boundary cursors in `CursorPage`.

## `obele.orm.signals`

`_Receiver` is the callback type alias. A `Signal` stores `name`,
documentation-only `providing_args`, receiver lists keyed by sender identity,
and a mutex.

`connect()` optionally wraps functions or bound methods in weak references and
registers them globally or for one sender. `disconnect()` resolves weak refs,
removes an identity match, and reports success. `send()` snapshots global and
sender-specific callbacks under the lock, skips collected callbacks, invokes
the rest as `receiver(sender, **kwargs)`, and returns callback/result pairs.
`has_receivers()` checks global and optional sender lists. `__repr__()` displays
name and registered count.

`pre_save`, `post_save`, `pre_delete`, `post_delete`, `pre_create`, and
`post_create` are shared `Signal` instances with documented argument names.
`receiver()` normalizes one signal or a list/tuple and returns a decorator that
connects the function to each.

## `obele.orm.mixins`

`_utcnow()` returns timezone-aware UTC now. `TimestampMixin` contributes
indexed nullable `created_at` and `updated_at` fields. Its `save()` initializes
creation time for new objects, updates modification time, and calls `super()`.

`SoftDeleteMixin` contributes indexed `is_deleted` and nullable `deleted_at`.
`delete()` marks and saves; `restore()` clears and saves; `arestore()` delegates
through `awrite`. `hard_delete()` invokes `Model.delete()` directly and
`ahard_delete()` delegates. `_queryset()` applies the default live-row filter;
`with_deleted()` returns an unfiltered queryset; `only_deleted()` selects
deleted rows.

## `obele.orm.search`

`SearchIndex.__init__()` stores the model/fields/tokenizer/sync mode, validates
the FTS table identifier, validates every source field, and records physical
column names. `_create_sqls()` returns either standalone FTS DDL or
external-content FTS DDL plus insert/delete/update synchronization triggers.

`create`/`acreate` execute that DDL. `drop`/`adrop` remove triggers and table.
`rebuild`/`arebuild` issue the FTS5 rebuild command. `optimize`/`aoptimize`
issue its optimize command. `search()` rejects blank queries, joins FTS rowid
to the model PK, orders by rank, applies optional limit/offset, and hydrates
models. `asearch()` delegates. `search_count()` and `asearch_count()` return
only the count. `__repr__()` shows model, table, and fields.

## `obele.kv.store`

### Types, constants, and helper records

`_Dumps` and `_Loads` describe custom serializers. `SerializerMode` accepts
`auto`, `json`, or `pickle`; `MultiGetReturn` accepts `dict` or `tuple`.
`_MISSING` distinguishes absent arguments/stored sentinel checks.

`_SORTABLE_KEY_TYPES` maps exact Python types to format tags;
`_SORTABLE_KEY_FORMATS` is its reverse; `_SORTABLE_COLUMNS` maps tags to native
ordering columns. `_NOT_EXPIRED` is the shared SQL predicate.

`_ensure_hashable()` rejects non-hashable keys. `_json_safe_encode()` emits
compact JSON only when decoding preserves both exact type and equality.
`_prefix_upper()` computes the first Unicode string above an entire prefix
range while skipping surrogate code points; it returns `None` for unbounded
prefixes.

`_EncodedKey` is an immutable slotted record containing physical lookup key,
format/payload, and one optional native sort value. `_EncodedValue` stores
format and bytes. `_upsert_sql()` is an LRU-cached, per-table UPSERT template.

### `KVStore` initialization and table methods

`__init__()` validates the table, stores type/namespace/serializer policy,
validates declared key types and serializer mode, creates the table/indexes,
and discovers an existing namespace's key type.

`table_name`, `key_type`, and `namespace` are read-only properties.
`__repr__()` reports the configuration. `_ensure_table()` transactionally
creates the `WITHOUT ROWID` table, namespace index, and four partial sort
indexes. `_load_existing_key_type()` inspects distinct formats and rejects
incompatible/mixed stored types under enforcement. `create_table` and
`acreate_table` ensure the schema; `drop_table` and `adrop_table` remove it.

### `KVStore` encoding and query helpers

`_encode_key()` checks hashability, chooses strict/flexible encoding, and
prefixes the physical identity for namespaces. `_encode_sortable_key()` fixes
or enforces one exact sortable type. `_encode_flexible_key()` uses native
encodings where possible and pickle otherwise. `_build_sortable_key()` creates
type-tagged binary identity and fills the matching native sort column; floats
must be finite.

`_encode_value()` selects custom, lossless JSON, or pickle. `_decode_value()`
dispatches by stored format. `_decode_key()` reconstructs native/pickled keys.
`_decode_pair()` combines both. `_format_pairs()` selects dictionary or tuple
output and validates the option.

`_now()` returns Unix time. `_delete_by_lookup()` deletes one physical key in
the namespace. `_live_row()` loads a key and lazily deletes it when expired.
`_select_rows()` chooses the native sort column when possible, excludes expired
rows, and applies direction/limit.

### Mapping and dictionary methods

`__getitem__()` performs a lookup or interprets a slice as a range.
`__setitem__()` calls `set`; `__delitem__()` calls `delete`; `__contains__()`
returns false for invalid/missing/expired keys and slices. `__len__()` counts
live namespace rows; `__bool__()` checks one. `__iter__()` yields sorted keys;
`__aiter__()` yields a materialized async key list.

`get()` returns a default on `KeyError`. `set()` encodes key/value/TTL and
executes the cached UPSERT. `delete()` raises when no physical row was removed.
`pop()`, `popitem()`, and `setdefault()` use transactions for atomic
read-modify-write behavior. `update()` normalizes mappings, pair iterables, and
kwargs into `set_many()`. `clear()` removes the current namespace. `keys()`,
`values()`, and `items()` return key-ordered lists.

### TTL and atomic methods

`ttl()` returns remaining seconds or `None` for persistence and raises when
missing. `expire()` applies a new absolute deadline only to a live key.
`persist()` clears a live key's deadline. `purge_expired()` physically removes
all expired namespace rows.

`increment()` transactionally reads a numeric value (zero when missing),
rejects booleans/non-numbers, writes the sum, and returns it.
`compare_and_swap()` transactionally requires an existing equal value before
writing the replacement and optional TTL.

### Batch, range, prefix, and scan methods

`set_many()` encodes all pairs and calls atomic `Database.executemany()`.
`delete_many()` uses one `IN` delete and returns its row count. `get_many()`
loads all physical keys in one query, filters expired rows, reconstructs input
order, and handles missing keys by error, default, or skip policy.

`range()` requires enforced sortable keys, validates step/bounds, uses
inclusive start and exclusive stop, orders/reverses, applies a positive Python
step, and formats output. `keys_slice()` and `values_slice()` project its tuple
pairs.

`_prefix_where()` creates an indexed lower/upper key-text range and validates
string-key compatibility. `prefix()` loads pairs with optional direction and
limit. `prefix_keys()` avoids loading values. `prefix_count()` counts.
`prefix_delete()` deletes the same range without an expiry condition.
`scan()` applies a caller-supplied SQLite GLOB pattern to live string keys.

### Memoization, statistics, and async methods

`memoize()` returns a decorator. Its local `cache_key()` combines prefix,
argument repr, and sorted keyword repr. It selects a coroutine wrapper for
async functions and a normal wrapper otherwise; both check `_MISSING`, call the
original on a miss, and save with optional TTL.

The coroutine path is the local `async_wrapper()`, which uses `aget()` and
`aset()` so neither cache access blocks the event loop. The synchronous path is
the local `wrapper()`, which performs the identical flow through `get()` and
`set()`. `functools.wraps()` preserves the decorated function's metadata.

`stats()` counts all, expired, and active namespace keys; groups key formats;
and returns table, namespace, serializer, resolved type, and enforcement data.

Every remaining `a`-prefixed method delegates the matching sync behavior via
`athread` for reads or `awrite` for writes:

- lookup/mapping: `aget`, `aset`, `adelete`, `apop`, `apopitem`,
  `asetdefault`, `aupdate`, `aclear`, `akeys`, `avalues`, `aitems`, `alen`,
  `acontains`;
- TTL/atomic: `attl`, `aexpire`, `apersist`, `apurge_expired`, `aincrement`,
  `acompare_and_swap`;
- batch: `aset_many`, `adelete_many`, `aget_many`;
- ordered/pattern: `arange`, `akeys_slice`, `avalues_slice`, `aprefix`,
  `aprefix_keys`, `aprefix_count`, `aprefix_delete`, `ascan`;
- monitoring: `astats`.

## `obele.kv.globals`

`KV` is a `KVStore` singleton. Class variable `_instance` holds the object and
`_lock` guards creation/reset. `__new__()` uses an unlocked fast path followed
by double-checked locking. `__init__()` uses `_kv_initialized` to ensure only
the first constructor call configures the inherited store. `reset()` clears
the class reference under the lock. `__repr__()` handles the brief
uninitialized state, otherwise shows table, namespace, size, type, and
enforcement.

## `obele.orm.cli`

`_coerce_scalar()` parses true, false, null, integer, float, then string.
`_parse_key_value()` validates `KEY=VALUE`; `_parse_rename_spec()` validates
`MODEL.FIELD=OLD_COLUMN`. `_qualified_model_name()` emits
`module:ClassName`.

`_import_modules()` imports names and converts `ImportError` to `ValueError`.
`_models_from_module()` selects concrete `Model` subclasses declared in that
module. `_resolve_model_spec()` accepts colon or dotted class syntax and
validates the result. `_dedupe_models()` preserves first occurrence by
qualified name. `_discover_models()` combines module discovery and explicit
specs and requires at least one result.

`_resolve_model_reference()` matches short or qualified rename references and
rejects missing/ambiguous results. `_group_renames()` validates fields and
builds a per-model rename map. `_build_parser()` constructs the command tree
and all options. Its local `add_model_args()` adds the shared module/model
selectors and, for migration, database/pragma options. `_ensure_selection()`
requires a module/model selector.
`_parse_pragmas()` parses repeated pragma options.

`_run_list_models()` discovers, dependency-sorts, and prints models.
`_run_migrate()` discovers/sorts, parses renames/pragmas, configures the DB,
migrates each model, prints progress unless quiet, and closes the current
connection. `main()` parses arguments, dispatches commands, and converts
`ValueError` into `argparse` errors. `__all__` exposes only `main`.

## How to Read Changes Safely

When modifying a subsystem, follow its dependency direction:

- database changes can affect every module;
- field/metaclass changes affect DDL, hydration, queries, relationships, and
  migration;
- query compiler changes affect model proxies, pagination, prefetch, and
  expression updates;
- model lifecycle changes affect signals and mixins;
- KV and search changes are mostly isolated above `Database`, but share its
  transaction and async semantics.

The test modules mirror these public behaviors. Run `pytest -q` after code
changes and `mkdocs build --strict` after code or documentation changes.
