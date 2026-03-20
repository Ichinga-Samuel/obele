"""Async test suite for obele.kv.

Mirrors the sync tests in test_kv.py using the ``a``-prefixed async API.
"""
import pytest

from obele import Database, KVStore, KV


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def setup_db():
    """Create a fresh in-memory database for each test."""
    Database.configure(":memory:")
    yield
    KV.reset()
    Database.close()


def _make_store(**kwargs) -> KVStore:
    return KVStore(**kwargs)


# ---------------------------------------------------------------------------
# Table Management
# ---------------------------------------------------------------------------

class TestAsyncTableManagement:
    async def test_drop_and_recreate(self):
        store = _make_store(table_name="async_t")
        await store.adrop_table()
        await store.acreate_table()
        cursor = Database.execute_read(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='async_t'"
        )
        assert cursor.fetchone() is not None


# ---------------------------------------------------------------------------
# Async CRUD
# ---------------------------------------------------------------------------

class TestAsyncCRUD:
    async def test_set_and_get(self):
        store = _make_store()
        await store.aset("hello", "world")
        result = await store.aget("hello")
        assert result == "world"

    async def test_delete(self):
        store = _make_store()
        await store.aset("key", "val")
        await store.adelete("key")
        assert await store.acontains("key") is False

    async def test_delete_missing_raises(self):
        store = _make_store()
        with pytest.raises(KeyError):
            await store.adelete("nope")

    async def test_contains(self):
        store = _make_store()
        await store.aset("x", 1)
        assert await store.acontains("x") is True
        assert await store.acontains("y") is False

    async def test_len(self):
        store = _make_store()
        await store.aset("a", 1)
        await store.aset("b", 2)
        assert await store.alen() == 2


# ---------------------------------------------------------------------------
# Async Extended Interface
# ---------------------------------------------------------------------------

class TestAsyncDictInterface:
    async def test_get_default(self):
        store = _make_store()
        assert await store.aget("missing", 42) == 42

    async def test_pop(self):
        store = _make_store()
        await store.aset("k", "val")
        result = await store.apop("k")
        assert result == "val"
        assert await store.acontains("k") is False

    async def test_pop_default(self):
        store = _make_store()
        assert await store.apop("nope", "fb") == "fb"

    async def test_popitem(self):
        store = _make_store()
        await store.aset("a", 1)
        await store.aset("b", 2)
        key, value = await store.apopitem(last=True)
        assert key == "b"
        assert value == 2

    async def test_setdefault(self):
        store = _make_store()
        result = await store.asetdefault("k", 100)
        assert result == 100
        assert await store.aget("k") == 100

    async def test_update(self):
        store = _make_store()
        await store.aupdate({"a": 1, "b": 2})
        assert await store.aget("a") == 1
        assert await store.aget("b") == 2

    async def test_clear(self):
        store = _make_store()
        await store.aset("x", 1)
        await store.aclear()
        assert await store.alen() == 0

    async def test_keys(self):
        store = _make_store()
        await store.aset("b", 2)
        await store.aset("a", 1)
        assert await store.akeys() == ["a", "b"]

    async def test_values(self):
        store = _make_store()
        await store.aset("a", 10)
        await store.aset("b", 20)
        assert await store.avalues() == [10, 20]

    async def test_items(self):
        store = _make_store()
        await store.aset("a", 10)
        await store.aset("b", 20)
        assert await store.aitems() == [("a", 10), ("b", 20)]


# ---------------------------------------------------------------------------
# Async Multi-Key & Batch
# ---------------------------------------------------------------------------

class TestAsyncMultiKeyBatch:
    async def test_get_many(self):
        store = _make_store()
        await store.aset("a", 1)
        await store.aset("b", 2)
        result = await store.aget_many("a", "b")
        assert result == {"a": 1, "b": 2}

    async def test_set_many(self):
        store = _make_store()
        await store.aset_many({"x": 10, "y": 20})
        assert await store.aget("x") == 10
        assert await store.alen() == 2

    async def test_delete_many(self):
        store = _make_store()
        await store.aset_many({"a": 1, "b": 2, "c": 3})
        deleted = await store.adelete_many(["a", "c"])
        assert deleted == 2
        assert await store.alen() == 1

