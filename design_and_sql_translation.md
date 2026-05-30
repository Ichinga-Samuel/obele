# Obele: Design Decisions and SQL Translation Deep Dive

This document drills down into the core design decisions of the `obele` library and explains exactly how the ORM translates Python code into raw SQLite queries.

## 1. Core Design Decisions

### Thread-Safety vs. Async Concurrency
`obele` was designed to be used in modern Python environments, which often mix synchronous threads and asynchronous `asyncio` event loops.
- **Per-Thread Read Connections:** SQLite running in Write-Ahead Log (WAL) mode supports concurrent readers. `Database.get_connection()` uses `threading.local()` to issue a unique SQLite connection to each thread. This allows multiple `SELECT` queries to run genuinely concurrently without blocking one another.
- **Global Write Lock:** SQLite does not support concurrent writes. To prevent `SQLITE_BUSY` errors, all write operations (`execute`, `save()`, `update()`, `delete()`) acquire a global `threading.RLock()` before executing `BEGIN IMMEDIATE`. This explicitly serializes writes while allowing reads to proceed uninterrupted.
- **Universal Async Proxies:** Rather than using a complex asynchronous SQLite driver, `obele` uses Python's standard `sqlite3` library. The `a`-prefixed methods (like `aexecute`, `acreate`, `asave`) simply use `asyncio.to_thread` to push the synchronous call into a thread pool, freeing the main event loop.

### Dirty Tracking and Snapshotting
When you retrieve a `Model` instance from the database, the `__init__` method (via `_from_row`) takes a snapshot of all non-primary-key field values.
- When you call `save()`, `obele` compares the current `__dict__` values against the snapshot (`self.dirty_fields`). 
- It only generates an `UPDATE` statement containing the fields that actually changed. If nothing changed, the query is skipped entirely. This minimizes database locking duration and I/O.

### Implicit vs Explicit Schema Migrations
Unlike Django or Alembic which track schema history, `obele` relies heavily on SQLite's unique characteristics. 
- The `Model.migrate()` method creates a shadow table (e.g. `users__old`), creates the new schema based purely on the Python class definition, copies data over using `INSERT INTO ... SELECT ...`, and drops the old table. This is safe, transactional, and stateless.

### The Key-Value Store (`KVStore`)
Sometimes, structured ORM tables are too rigid. `obele` includes a fast, schema-less `KV` store (backed by a single SQLite table with `key`, `value`, `namespace`, `expires_at`).
- Values are automatically serialized to JSON.
- Expiration is checked at read-time (`is_expired`), avoiding the need for background sweeper threads.

---

## 2. How Queries are Translated into SQL

The translation of Python method calls into SQL happens primarily within the `QuerySet` class (in `obele.orm.query`).

### Phase 1: The Lazy Builder (`QuerySet`)
When you call `User.filter(age__gte=18).order_by('-name')`, no SQL is executed. Instead, `QuerySet` clones itself and records your intent in internal state variables:
- `_where_fragments`: A list of tuples `(sql_string, params)`.
- `_order_fields`: A list of strings (e.g., `["name DESC"]`).
- `_join_specs`: A list of `_JoinSpec` dataclasses that describe how to link tables.

### Phase 2: Resolving Expressions (`Q`, `F`, `Func`)
`obele` supports complex expressions like `Q(name="Alice") | Q(age=30)`.
- **`Q` Objects**: Represent boolean logic trees. The `_compile_q` method recursively walks the `Q` tree. An `|` (OR) operator stitches the children together with ` OR `, wrapping them in parentheses.
- **Double-Underscore Lookups**: When `_compile_condition` encounters `age__gte=18`, it splits the string into a path (`age`) and a lookup (`gte`). It maps `gte` to `>= ?` via the `_LOOKUPS` dictionary.
- **LIKE Escaping**: Lookups like `icontains` translate to `LOWER(column) LIKE ? ESCAPE '\'`. The Python value has `%` wrapped around it and any literal `%` or `_` are explicitly escaped.

### Phase 3: Automatic Joins (`_resolve_field_reference`)
If a lookup traverses a relation (e.g., `author__company__name__exact="Acme"`), `_resolve_field_reference` loops through the path segments.
- For each segment, it calls `_ensure_join()`, which inspects the `ForeignKeyField` (or reverse relation).
- It generates an alias (like `jt0`, `jt1`) and builds an `INNER` or `LEFT JOIN` SQL string.
- These joins are stored in `_join_map` to prevent duplicate joins if you filter on multiple fields across the same relationship.

### Phase 4: Constructing the SQL String (`_build_select`)
When you finally materialize the query (e.g., by calling `.all()`, `.first()`, or iterating it), `_build_select()` triggers:
1. **SELECT**: It looks at `.only()`, `.defer()`, or `.values()` to decide which columns to project. If there are `.annotate()` expressions, their SQL strings are appended with `AS alias`.
2. **FROM**: Appends the model's `table_name`.
3. **JOIN**: Appends all the generated SQL from `_join_specs`.
4. **WHERE**: Joins all `_where_fragments` with `AND`.
5. **GROUP BY / HAVING**: Appends if `.group_by()` or `.having()` were used.
6. **ORDER BY / LIMIT / OFFSET**: Appends pagination and sorting clauses.

The method returns a raw `(sql_string, parameters)` tuple.

### Phase 5: Execution and Hydration (`iterator`)
1. The SQL string and parameters are passed to `Database.execute_read()`.
2. As rows stream back from SQLite via `.fetchmany()`, the `_row_to_instance()` method is called for each row.
3. This method maps the dictionary columns back into a `Model` instance. 
4. If `.select_related()` was used, it parses out the `jt0.column_name` aliases to hydrate the nested Python objects in a single pass, preventing N+1 query problems.

## Summary

`obele` translates queries into SQL by building an AST (Abstract Syntax Tree) using `Q` and `Expression` objects, resolving dotted paths into automatic SQL `JOIN` clauses, and lazily stitching together a final query string only at the exact moment execution is requested. Its thread-safe database wrapper guarantees that reads remain concurrent while writes are safely serialized to prevent SQLite contention.
