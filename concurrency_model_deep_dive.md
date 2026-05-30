# Obele Concurrency Model Deep Dive

`obele` uses a highly deliberate concurrency model designed to maximize throughput for concurrent readers (which is the majority of database traffic) while providing strict safety and serialization for writers. It does this without requiring an asynchronous SQLite driver, instead relying on Python's threading and `ContextVar` primitives combined with SQLite's WAL (Write-Ahead Log) mode.

## 1. The Core Problem with SQLite Concurrency

By default, SQLite locks the entire database file when writing. If multiple connections try to write simultaneously, one will throw an `SQLITE_BUSY` error. Additionally, Python's `sqlite3` module objects (connections and cursors) are generally not safe to share across threads.

In an asynchronous web framework (like FastAPI or Starlette), dozens of concurrent requests might hit the database simultaneously. If an ORM doesn't handle this properly, the application will frequently crash with `OperationalError: database is locked`.

## 2. Obele's Solution: Read Concurrency + Write Serialization

`obele` implements a hybrid model located in `obele.orm.database.Database` to solve this perfectly:

### Concurrent Reads via Thread-Locals
When SQLite is configured in `WAL` mode (which `obele` sets automatically via `PRAGMA journal_mode=WAL`), SQLite allows simultaneous readers even if a writer is active.
- To utilize this, `obele` issues a *unique* SQLite connection to every thread that requests one.
- It stores this connection in a `threading.local()` object (`Database._local.connection`).
- When Thread A and Thread B both issue a `SELECT` query, they use entirely different underlying SQLite connections. Because they are separate connections, SQLite allows them to read concurrently without blocking one another.

### Serialized Writes via Global RLock
To prevent `SQLITE_BUSY` errors entirely, `obele` forces all write operations (like `execute`, `save`, `update`, `delete`) to pass through a single, global re-entrant lock: `Database._write_lock`.
- When a thread wants to write, it acquires the `_write_lock`.
- It executes `BEGIN IMMEDIATE` (which tells SQLite this connection is starting a write transaction).
- It performs the query, commits, and releases the `_write_lock`.
- If a second thread tries to write at the same time, it simply waits at the Python level (on `_write_lock.acquire()`) rather than failing at the SQLite level. This ensures you never get a database locked error.

## 3. Async Boundaries and ContextVars

While `threading.local` is great for WSGI (synchronous) apps, modern ASGI (asynchronous) apps use `asyncio`. In `asyncio`, tasks can jump between different threads in a thread pool (via `asyncio.to_thread`).

To prevent transaction bleed across async task boundaries, `obele` uses modern Python `ContextVar`s:
- `_scoped_binding = ContextVar(...)`
- `_transaction_connection = ContextVar(...)`

When you enter an explicit transaction (e.g., `async with Database.transaction():`), `obele` allocates a connection and pins it to the `ContextVar`. `ContextVar` is natively understood by `asyncio`. Even if the async task is suspended (`await`) and resumes on a completely different OS thread in the pool, `obele` will correctly look up the `ContextVar`, retrieve the exact same SQLite connection, and continue the transaction safely.

## 4. The Async API Implementation

All `a`-prefixed methods (like `aexecute_read`, `asave`, `afirst`) are implemented very simply:
```python
@classmethod
async def aexecute_read(cls, sql: str, params=None):
    return await asyncio.to_thread(cls.execute_read, sql, params)
```
**Why this works beautifully:**
1. You call `await User.filter(name="Alice").aall()`.
2. Python dispatches the work to a background thread pool via `asyncio.to_thread`.
3. The background thread picks up the task, sees it needs a read connection, and grabs its `threading.local` connection (creating one if it doesn't exist).
4. It executes the query (concurrently with any other thread).
5. It returns the result back to your async event loop.

The main `asyncio` loop is never blocked by database I/O, and because of the thread-local pooling, connection overhead is minimized.

## 5. Summary of Locks in Obele

If you read the source code for `Database`, you will see three distinct locks:
1. **`_lock`**: Protects the connection pool metadata (dictionaries mapping connection IDs to creation times). Acquired only for microseconds when a connection is opened or closed.
2. **`_write_lock`**: The global lock protecting SQLite writes. Acquired during `INSERT/UPDATE/DELETE`.
3. **`_operation_lock(conn)`**: A per-connection lock. This is only used when multiple async tasks try to use the *same* explicit connection (like inside a shared `Database.transaction()`) to prevent interleaving SQLite commands on the same socket.
