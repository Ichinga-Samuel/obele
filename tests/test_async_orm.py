"""Comprehensive async test suite for obele.

Mirrors the sync test structure in test_orm.py but exercises every
``async`` / ``a``-prefixed method in Database, Model, and QuerySet.

Uses an in-memory SQLite database for every test to ensure isolation.
"""
import datetime

import pytest
import pytest_asyncio

from obele import (
    Database,
    Model,
    IntegerField,
    TextField,
    RealField,
    BooleanField,
    DateTimeField,
    ForeignKeyField,
    FieldValidationError,
    RecordNotFoundError,
    MultipleResultsError,
    IntegrityError,
)


# ---------------------------------------------------------------------------
# Test models (reuse-safe â€” metaclass only runs once per class)
# ---------------------------------------------------------------------------

class AUser(Model):
    table_name = "ausers"
    name = TextField()
    email = TextField(unique=True)
    age = IntegerField(nullable=True)
    score = RealField(nullable=True)
    active = BooleanField(default=True)


class APost(Model):
    table_name = "aposts"
    title = TextField()
    body = TextField(nullable=True)
    author = ForeignKeyField(to=AUser)
    created_at = DateTimeField(nullable=True)


class ATag(Model):
    table_name = "atags"
    label = TextField(max_length=50, unique=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Create a fresh in-memory database for each async test."""
    await Database.aconfigure(":memory:")
    yield
    await Database.aclose()


async def _seed_users():
    """Insert five users for query tests."""
    await AUser.acreate_table()
    await AUser.acreate(name="Alice", email="alice@test.com", age=30, score=85.0)
    await AUser.acreate(name="Bob", email="bob@test.com", age=25, score=90.0)
    await AUser.acreate(name="Carol", email="carol@test.com", age=35, score=78.0)
    await AUser.acreate(name="Dave", email="dave@test.com", age=28, score=92.0)
    await AUser.acreate(name="Eve", email="eve@test.com", age=22, score=88.0)


# ---------------------------------------------------------------------------
# Database Async API Tests
# ---------------------------------------------------------------------------

class TestAsyncDatabase:
    @pytest.mark.asyncio
    async def test_aconfigure(self):
        """aconfigure should set up a working connection."""
        await Database.aconfigure(":memory:")
        conn = Database.get_connection()
        assert conn is not None

    @pytest.mark.asyncio
    async def test_aexecute_and_aexecute_read(self):
        """aexecute (write) and aexecute_read (read) should round-trip data."""
        await Database.aexecute(
            "CREATE TABLE _test (id INTEGER PRIMARY KEY, val TEXT)"
        )
        await Database.aexecute(
            "INSERT INTO _test (val) VALUES (?)", ["hello"]
        )
        cursor = await Database.aexecute_read("SELECT val FROM _test")
        row = cursor.fetchone()
        assert row["val"] == "hello"

    @pytest.mark.asyncio
    async def test_aexecutemany(self):
        """aexecutemany should insert multiple rows in a single call."""
        await Database.aexecute(
            "CREATE TABLE _bulk (id INTEGER PRIMARY KEY, n INTEGER)"
        )
        await Database.aexecutemany(
            "INSERT INTO _bulk (n) VALUES (?)", [[i] for i in range(5)]
        )
        cursor = await Database.aexecute_read("SELECT COUNT(*) AS cnt FROM _bulk")
        assert cursor.fetchone()["cnt"] == 5

    @pytest.mark.asyncio
    async def test_aclose(self):
        """aclose should close the connection."""
        await Database.aconfigure(":memory:")
        await Database.aclose()
        assert Database._connection is None

    @pytest.mark.asyncio
    async def test_async_context_manager(self):
        """Database should be usable as an async context manager."""
        db = Database()
        async with db:
            conn = Database.get_connection()
            assert conn is not None
        # Connection should be closed after exiting
        assert Database._connection is None


# ---------------------------------------------------------------------------
# Schema Tests
# ---------------------------------------------------------------------------

class TestAsyncSchema:
    @pytest.mark.asyncio
    async def test_acreate_table(self):
        await AUser.acreate_table()
        cursor = Database.execute_read(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ausers'"
        )
        assert cursor.fetchone() is not None

    @pytest.mark.asyncio
    async def test_adrop_table(self):
        await AUser.acreate_table()
        await AUser.adrop_table()
        cursor = Database.execute_read(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ausers'"
        )
        assert cursor.fetchone() is None

    @pytest.mark.asyncio
    async def test_acreate_table_if_not_exists(self):
        await AUser.acreate_table()
        # Should not raise
        await AUser.acreate_table(if_not_exists=True)


# ---------------------------------------------------------------------------
# CRUD Tests
# ---------------------------------------------------------------------------

class TestAsyncCRUD:
    @pytest.mark.asyncio
    async def test_acreate_and_aget(self):
        await AUser.acreate_table()
        u = await AUser.acreate(name="Alice", email="alice@test.com", age=30)
        fetched = await AUser.aget(id=u.id)
        assert fetched.name == "Alice"
        assert fetched.age == 30

    @pytest.mark.asyncio
    async def test_asave_insert(self):
        await AUser.acreate_table()
        u = AUser(name="Alice", email="alice@test.com")
        await u.asave()
        assert u.id is not None
        fetched = await AUser.aget(id=u.id)
        assert fetched.name == "Alice"

    @pytest.mark.asyncio
    async def test_asave_update(self):
        await AUser.acreate_table()
        u = await AUser.acreate(name="Alice", email="alice@test.com")
        u.name = "Alicia"
        await u.asave()
        fetched = await AUser.aget(id=u.id)
        assert fetched.name == "Alicia"

    @pytest.mark.asyncio
    async def test_adelete(self):
        await AUser.acreate_table()
        u = await AUser.acreate(name="Alice", email="alice@test.com")
        uid = u.id
        await u.adelete()
        assert u.id is None
        with pytest.raises(RecordNotFoundError):
            await AUser.aget(id=uid)

    @pytest.mark.asyncio
    async def test_arefresh(self):
        await AUser.acreate_table()
        u = await AUser.acreate(name="Alice", email="alice@test.com")
        # Modify via raw SQL
        await Database.aexecute(
            "UPDATE ausers SET name='Updated' WHERE id=?", [u.id]
        )
        await u.arefresh()
        assert u.name == "Updated"

    @pytest.mark.asyncio
    async def test_aget_or_create_existing(self):
        await AUser.acreate_table()
        await AUser.acreate(name="Alice", email="alice@test.com")
        u, created = await AUser.aget_or_create(
            name="Alice", email="alice@test.com"
        )
        assert not created
        assert u.name == "Alice"

    @pytest.mark.asyncio
    async def test_aget_or_create_new(self):
        await AUser.acreate_table()
        u, created = await AUser.aget_or_create(
            email="new@test.com", defaults={"name": "NewUser"}
        )
        assert created
        assert u.name == "NewUser"

    @pytest.mark.asyncio
    async def test_abulk_create(self):
        await AUser.acreate_table()
        items = [
            {"name": f"User{i}", "email": f"user{i}@test.com", "age": 20 + i}
            for i in range(5)
        ]
        users = await AUser.abulk_create(items)
        assert len(users) == 5
        assert await AUser.acount() == 5

    @pytest.mark.asyncio
    async def test_unique_constraint_async(self):
        await AUser.acreate_table()
        await AUser.acreate(name="Alice", email="alice@test.com")
        with pytest.raises(IntegrityError):
            await AUser.acreate(name="Bob", email="alice@test.com")

    @pytest.mark.asyncio
    async def test_to_dict_after_acreate(self):
        await AUser.acreate_table()
        u = await AUser.acreate(name="Alice", email="alice@test.com", age=25)
        d = u.to_dict()
        assert d["name"] == "Alice"
        assert d["age"] == 25

    @pytest.mark.asyncio
    async def test_datetime_field_async_round_trip(self):
        await AUser.acreate_table()
        await APost.acreate_table()
        u = await AUser.acreate(name="Dave", email="dave@test.com")
        now = datetime.datetime(2025, 6, 15, 12, 30, 0)
        p = await APost.acreate(title="Hello", author=u.id, created_at=now)
        await p.arefresh()
        assert p.created_at == now


# ---------------------------------------------------------------------------
# QuerySet Async Tests
# ---------------------------------------------------------------------------

class TestAsyncQuerySet:
    @pytest.mark.asyncio
    async def test_aall(self):
        await _seed_users()
        users = await AUser.aall()
        assert len(users) == 5

    @pytest.mark.asyncio
    async def test_filter_aall(self):
        await _seed_users()
        results = await AUser.filter(name="Alice").aall()
        assert len(results) == 1
        assert results[0].name == "Alice"

    @pytest.mark.asyncio
    async def test_filter_gt_aall(self):
        await _seed_users()
        results = await AUser.filter(age__gt=28).aall()
        assert all(u.age > 28 for u in results)
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_filter_in_aall(self):
        await _seed_users()
        results = await AUser.filter(name__in=["Alice", "Bob"]).aall()
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_exclude_aall(self):
        await _seed_users()
        results = await AUser.exclude(name="Alice").aall()
        assert len(results) == 4
        assert all(u.name != "Alice" for u in results)

    @pytest.mark.asyncio
    async def test_order_by_aall(self):
        await _seed_users()
        results = await AUser.order_by("age").aall()
        ages = [u.age for u in results]
        assert ages == sorted(ages)

    @pytest.mark.asyncio
    async def test_order_by_desc_aall(self):
        await _seed_users()
        results = await AUser.order_by("-age").aall()
        ages = [u.age for u in results]
        assert ages == sorted(ages, reverse=True)

    @pytest.mark.asyncio
    async def test_limit_aall(self):
        await _seed_users()
        results = await AUser.limit(3).aall()
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_offset_aall(self):
        await _seed_users()
        all_users = await AUser.order_by("id").aall()
        offset_users = await AUser.order_by("id").offset(2).aall()
        assert offset_users[0].id == all_users[2].id

    @pytest.mark.asyncio
    async def test_afirst(self):
        await _seed_users()
        u = await AUser.afirst()
        assert u is not None

    @pytest.mark.asyncio
    async def test_afirst_empty(self):
        await AUser.acreate_table()
        assert await AUser.afirst() is None

    @pytest.mark.asyncio
    async def test_aget_success(self):
        await _seed_users()
        u = await AUser.aget(name="Alice")
        assert u.name == "Alice"

    @pytest.mark.asyncio
    async def test_aget_not_found(self):
        await _seed_users()
        with pytest.raises(RecordNotFoundError):
            await AUser.aget(name="Zara")

    @pytest.mark.asyncio
    async def test_aget_multiple_results(self):
        await AUser.acreate_table()
        await AUser.acreate(name="Same", email="s1@test.com", age=20)
        await AUser.acreate(name="Same", email="s2@test.com", age=21)
        with pytest.raises(MultipleResultsError):
            await AUser.aget(name="Same")

    @pytest.mark.asyncio
    async def test_acount(self):
        await _seed_users()
        assert await AUser.acount() == 5

    @pytest.mark.asyncio
    async def test_aexists(self):
        await _seed_users()
        assert await AUser.filter(name="Alice").aexists()
        assert not await AUser.filter(name="Zara").aexists()

    @pytest.mark.asyncio
    async def test_chained_queries_async(self):
        await _seed_users()
        results = await (
            AUser.filter(age__gte=25)
                .exclude(name="Bob")
                .order_by("-score")
                .limit(2)
                .aall()
        )
        assert len(results) <= 2
        assert all(u.age >= 25 for u in results)
        assert all(u.name != "Bob" for u in results)


# ---------------------------------------------------------------------------
# Aggregate Tests
# ---------------------------------------------------------------------------

class TestAsyncAggregates:
    async def _seed(self):
        await AUser.acreate_table()
        await AUser.acreate(name="A", email="a@test.com", age=20, score=80.0)
        await AUser.acreate(name="B", email="b@test.com", age=30, score=90.0)
        await AUser.acreate(name="C", email="c@test.com", age=40, score=100.0)

    @pytest.mark.asyncio
    async def test_aaggregate_sum(self):
        await self._seed()
        assert await AUser.aaggregate("SUM", "age") == 90

    @pytest.mark.asyncio
    async def test_aaggregate_avg(self):
        await self._seed()
        assert await AUser.aaggregate("AVG", "age") == 30.0

    @pytest.mark.asyncio
    async def test_aaggregate_min(self):
        await self._seed()
        assert await AUser.aaggregate("MIN", "score") == 80.0

    @pytest.mark.asyncio
    async def test_aaggregate_max(self):
        await self._seed()
        assert await AUser.aaggregate("MAX", "score") == 100.0

    @pytest.mark.asyncio
    async def test_aaggregate_count(self):
        await self._seed()
        assert await AUser.aaggregate("COUNT", "id") == 3

    @pytest.mark.asyncio
    async def test_filtered_aaggregate(self):
        await self._seed()
        result = await AUser.filter(age__gte=30).aaggregate("SUM", "age")
        assert result == 70


# ---------------------------------------------------------------------------
# Bulk Operations Tests
# ---------------------------------------------------------------------------

class TestAsyncBulkOps:
    @pytest.mark.asyncio
    async def test_aupdate(self):
        await AUser.acreate_table()
        await AUser.acreate(name="A", email="a@test.com", age=20, active=True)
        await AUser.acreate(name="B", email="b@test.com", age=30, active=True)
        await AUser.acreate(name="C", email="c@test.com", age=40, active=True)
        count = await AUser.filter(age__gte=30).aupdate(active=False)
        assert count == 2
        assert await AUser.filter(active=False).acount() == 2

    @pytest.mark.asyncio
    async def test_adelete_queryset(self):
        await AUser.acreate_table()
        await AUser.acreate(name="A", email="a@test.com", age=20)
        await AUser.acreate(name="B", email="b@test.com", age=30)
        await AUser.acreate(name="C", email="c@test.com", age=40)
        count = await AUser.filter(age__lt=30).adelete()
        assert count == 1
        assert await AUser.acount() == 2


# ---------------------------------------------------------------------------
# Foreign Key Tests
# ---------------------------------------------------------------------------

class TestAsyncForeignKey:
    @pytest.mark.asyncio
    async def test_fk_insert_and_query(self):
        await AUser.acreate_table()
        await APost.acreate_table()
        u = await AUser.acreate(name="Alice", email="alice@test.com")
        p = await APost.acreate(title="Hello", author=u.id)
        fetched = await APost.aget(id=p.id)
        assert fetched.author == u.id

    @pytest.mark.asyncio
    async def test_fk_cascade_delete(self):
        await AUser.acreate_table()
        await APost.acreate_table()
        u = await AUser.acreate(name="Alice", email="alice@test.com")
        await APost.acreate(title="Post1", author=u.id)
        await APost.acreate(title="Post2", author=u.id)
        await u.adelete()
        assert await APost.acount() == 0


# ---------------------------------------------------------------------------
# Async Iteration Tests
# ---------------------------------------------------------------------------

class TestAsyncIteration:
    @pytest.mark.asyncio
    async def test_aiter(self):
        """async for should yield all matching instances."""
        await _seed_users()
        names = []
        async for u in AUser.filter():
            names.append(u.name)
        assert set(names) == {"Alice", "Bob", "Carol", "Dave", "Eve"}

    @pytest.mark.asyncio
    async def test_aiter_filtered(self):
        """async for on a filtered queryset should only yield matches."""
        await _seed_users()
        names = []
        async for u in AUser.filter(age__gte=30):
            names.append(u.name)
        assert set(names) == {"Alice", "Carol"}


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------

class TestAsyncEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_table_query(self):
        await AUser.acreate_table()
        assert await AUser.aall() == []
        assert await AUser.acount() == 0
        assert not await AUser.aexists()

    @pytest.mark.asyncio
    async def test_adelete_unsaved_raises(self):
        await AUser.acreate_table()
        u = AUser(name="Test", email="test@test.com")
        with pytest.raises(RecordNotFoundError, match="unsaved"):
            await u.adelete()

    @pytest.mark.asyncio
    async def test_arefresh_unsaved_raises(self):
        await AUser.acreate_table()
        u = AUser(name="Test", email="test@test.com")
        with pytest.raises(RecordNotFoundError, match="unsaved"):
            await u.arefresh()

    @pytest.mark.asyncio
    async def test_field_validation_through_async(self):
        """Validation errors should propagate through async wrappers."""
        await ATag.acreate_table()
        with pytest.raises(FieldValidationError, match="max_length"):
            await ATag.acreate(label="x" * 51)

    @pytest.mark.asyncio
    async def test_boolean_default_async(self):
        await AUser.acreate_table()
        u = await AUser.acreate(name="Carol", email="carol@test.com")
        assert u.active is True

    @pytest.mark.asyncio
    async def test_nullable_field_async(self):
        await AUser.acreate_table()
        u = await AUser.acreate(name="Frank", email="frank@test.com", age=None)
        assert u.age is None

    @pytest.mark.asyncio
    async def test_repr_after_acreate(self):
        await AUser.acreate_table()
        u = await AUser.acreate(name="Alice", email="alice@test.com")
        assert "AUser" in repr(u)
        assert str(u.id) in repr(u)

    @pytest.mark.asyncio
    async def test_equality_after_aget(self):
        await AUser.acreate_table()
        u1 = await AUser.acreate(name="Alice", email="alice@test.com")
        u2 = await AUser.aget(id=u1.id)
        assert u1 == u2

