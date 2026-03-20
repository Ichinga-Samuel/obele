"""Comprehensive test suite for obele.

Uses an in-memory SQLite database for every test to ensure isolation.
"""
import datetime
import threading
import pytest

from obele import (
    Database,
    Model,
    IntegerField,
    TextField,
    RealField,
    BlobField,
    BooleanField,
    DateTimeField,
    ForeignKeyField,
    FieldValidationError,
    RecordNotFoundError,
    MultipleResultsError,
    IntegrityError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def setup_db():
    """Create a fresh in-memory database for each test."""
    Database.configure(":memory:")
    yield
    Database.close()


# ---------------------------------------------------------------------------
# Test models â€” declared at module level so metaclass only runs once
# ---------------------------------------------------------------------------

class User(Model):
    table_name = "users"
    name = TextField()
    email = TextField(unique=True)
    age = IntegerField(nullable=True)
    score = RealField(nullable=True)
    active = BooleanField(default=True)


class Post(Model):
    table_name = "posts"
    title = TextField()
    body = TextField(nullable=True)
    author = ForeignKeyField(to=User)
    created_at = DateTimeField(nullable=True)


class Tag(Model):
    table_name = "tags"
    label = TextField(max_length=50, unique=True)


# ---------------------------------------------------------------------------
# Field Validation Tests
# ---------------------------------------------------------------------------

class TestFields:
    def test_integer_field_accepts_int(self):
        User.create_table()
        u = User.create(name="Alice", email="alice@test.com", age=30)
        assert u.age == 30

    def test_text_field_max_length(self):
        Tag.create_table()
        with pytest.raises(FieldValidationError, match="max_length"):
            Tag.create(label="x" * 51)

    def test_boolean_field_stored_as_int(self):
        User.create_table()
        u = User.create(name="Bob", email="bob@test.com", active=False)
        u.refresh()
        assert u.active is False

    def test_boolean_field_default(self):
        User.create_table()
        u = User.create(name="Carol", email="carol@test.com")
        assert u.active is True

    def test_datetime_field_round_trip(self):
        User.create_table()
        Post.create_table()
        u = User.create(name="Dave", email="dave@test.com")
        now = datetime.datetime(2025, 6, 15, 12, 30, 0)
        p = Post.create(title="Hello", author=u.id, created_at=now)
        p.refresh()
        assert p.created_at == now

    def test_real_field_accepts_int(self):
        User.create_table()
        u = User.create(name="Eve", email="eve@test.com", score=95)
        assert isinstance(u.score, float)
        assert u.score == 95.0

    def test_nullable_field_accepts_none(self):
        User.create_table()
        u = User.create(name="Frank", email="frank@test.com", age=None)
        assert u.age is None

    def test_non_nullable_field_rejects_none(self):
        User.create_table()
        with pytest.raises(FieldValidationError, match="does not allow None"):
            User.create(name=None, email="x@test.com")


# ---------------------------------------------------------------------------
# Table Creation / Drop Tests
# ---------------------------------------------------------------------------

class TestSchema:
    def test_create_table(self):
        User.create_table()
        cursor = Database.execute_read(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        )
        assert cursor.fetchone() is not None

    def test_drop_table(self):
        User.create_table()
        User.drop_table()
        cursor = Database.execute_read(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        )
        assert cursor.fetchone() is None

    def test_create_table_if_not_exists(self):
        User.create_table()
        # Should not raise
        User.create_table(if_not_exists=True)

    def test_auto_pk(self):
        """Models without an explicit PK get an auto-increment ``id``."""
        User.create_table()
        u1 = User.create(name="A", email="a@test.com")
        u2 = User.create(name="B", email="b@test.com")
        assert u1.id == 1
        assert u2.id == 2


# ---------------------------------------------------------------------------
# CRUD Tests
# ---------------------------------------------------------------------------

class TestCRUD:
    def test_create_and_retrieve(self):
        User.create_table()
        u = User.create(name="Alice", email="alice@test.com", age=30)
        fetched = User.get(id=u.id)
        assert fetched.name == "Alice"
        assert fetched.age == 30

    def test_save_update(self):
        User.create_table()
        u = User.create(name="Alice", email="alice@test.com")
        u.name = "Alicia"
        u.save()
        fetched = User.get(id=u.id)
        assert fetched.name == "Alicia"

    def test_delete(self):
        User.create_table()
        u = User.create(name="Alice", email="alice@test.com")
        uid = u.id
        u.delete()
        assert u.id is None
        with pytest.raises(RecordNotFoundError):
            User.get(id=uid)

    def test_refresh(self):
        User.create_table()
        u = User.create(name="Alice", email="alice@test.com")
        # Modify via raw SQL
        Database.execute("UPDATE users SET name='Updated' WHERE id=?", [u.id])
        u.refresh()
        assert u.name == "Updated"

    def test_to_dict(self):
        User.create_table()
        u = User.create(name="Alice", email="alice@test.com", age=25)
        d = u.to_dict()
        assert d["name"] == "Alice"
        assert d["email"] == "alice@test.com"
        assert d["age"] == 25

    def test_get_or_create_existing(self):
        User.create_table()
        User.create(name="Alice", email="alice@test.com")
        u, created = User.get_or_create(name="Alice", email="alice@test.com")
        assert not created
        assert u.name == "Alice"

    def test_get_or_create_new(self):
        User.create_table()
        u, created = User.get_or_create(
            email="new@test.com", defaults={"name": "NewUser"}
        )
        assert created
        assert u.name == "NewUser"

    def test_bulk_create(self):
        User.create_table()
        items = [
            {"name": f"User{i}", "email": f"user{i}@test.com", "age": 20 + i}
            for i in range(5)
        ]
        users = User.bulk_create(items)
        assert len(users) == 5
        assert User.count() == 5

    def test_unique_constraint(self):
        User.create_table()
        User.create(name="Alice", email="alice@test.com")
        with pytest.raises(IntegrityError):
            User.create(name="Bob", email="alice@test.com")


# ---------------------------------------------------------------------------
# QuerySet Tests
# ---------------------------------------------------------------------------

class TestQuerySet:
    def _seed_users(self):
        User.create_table()
        User.create(name="Alice", email="alice@test.com", age=30, score=85.0)
        User.create(name="Bob", email="bob@test.com", age=25, score=90.0)
        User.create(name="Carol", email="carol@test.com", age=35, score=78.0)
        User.create(name="Dave", email="dave@test.com", age=28, score=92.0)
        User.create(name="Eve", email="eve@test.com", age=22, score=88.0)

    def test_all(self):
        self._seed_users()
        assert len(User.all()) == 5

    def test_filter_exact(self):
        self._seed_users()
        results = User.filter(name="Alice").all()
        assert len(results) == 1
        assert results[0].name == "Alice"

    def test_filter_gt(self):
        self._seed_users()
        results = User.filter(age__gt=28).all()
        assert all(u.age > 28 for u in results)
        assert len(results) == 2  # Alice(30), Carol(35)

    def test_filter_gte(self):
        self._seed_users()
        results = User.filter(age__gte=30).all()
        assert len(results) == 2

    def test_filter_lt(self):
        self._seed_users()
        results = User.filter(age__lt=25).all()
        assert len(results) == 1  # Eve(22)

    def test_filter_lte(self):
        self._seed_users()
        results = User.filter(age__lte=25).all()
        assert len(results) == 2  # Bob(25), Eve(22)

    def test_filter_ne(self):
        self._seed_users()
        results = User.filter(name__ne="Alice").all()
        assert len(results) == 4

    def test_filter_like(self):
        self._seed_users()
        results = User.filter(name__like="A%").all()
        assert len(results) == 1
        assert results[0].name == "Alice"

    def test_filter_in(self):
        self._seed_users()
        results = User.filter(name__in=["Alice", "Bob"]).all()
        assert len(results) == 2

    def test_filter_is_null(self):
        User.create_table()
        User.create(name="WithAge", email="wa@test.com", age=25)
        User.create(name="NoAge", email="na@test.com", age=None)
        results = User.filter(age__is_null=True).all()
        assert len(results) == 1
        assert results[0].name == "NoAge"

    def test_exclude(self):
        self._seed_users()
        results = User.exclude(name="Alice").all()
        assert len(results) == 4
        assert all(u.name != "Alice" for u in results)

    def test_order_by_asc(self):
        self._seed_users()
        results = User.order_by("age").all()
        ages = [u.age for u in results]
        assert ages == sorted(ages)

    def test_order_by_desc(self):
        self._seed_users()
        results = User.order_by("-age").all()
        ages = [u.age for u in results]
        assert ages == sorted(ages, reverse=True)

    def test_limit(self):
        self._seed_users()
        results = User.limit(3).all()
        assert len(results) == 3

    def test_offset(self):
        self._seed_users()
        all_users = User.order_by("id").all()
        offset_users = User.order_by("id").offset(2).all()
        assert offset_users[0].id == all_users[2].id

    def test_first(self):
        self._seed_users()
        u = User.first()
        assert u is not None

    def test_first_empty(self):
        User.create_table()
        assert User.first() is None

    def test_get_success(self):
        self._seed_users()
        u = User.get(name="Alice")
        assert u.name == "Alice"

    def test_get_not_found(self):
        self._seed_users()
        with pytest.raises(RecordNotFoundError):
            User.get(name="Zara")

    def test_get_multiple_results(self):
        User.create_table()
        User.create(name="Same", email="s1@test.com", age=20)
        User.create(name="Same", email="s2@test.com", age=21)
        with pytest.raises(MultipleResultsError):
            User.get(name="Same")

    def test_count(self):
        self._seed_users()
        assert User.count() == 5

    def test_exists(self):
        self._seed_users()
        assert User.filter(name="Alice").exists()
        assert not User.filter(name="Zara").exists()

    def test_chained_queries(self):
        self._seed_users()
        results = (
            User.filter(age__gte=25)
                .exclude(name="Bob")
                .order_by("-score")
                .limit(2)
                .all()
        )
        assert len(results) <= 2
        assert all(u.age >= 25 for u in results)
        assert all(u.name != "Bob" for u in results)


# ---------------------------------------------------------------------------
# Aggregate Tests
# ---------------------------------------------------------------------------

class TestAggregates:
    def _seed(self):
        User.create_table()
        User.create(name="A", email="a@test.com", age=20, score=80.0)
        User.create(name="B", email="b@test.com", age=30, score=90.0)
        User.create(name="C", email="c@test.com", age=40, score=100.0)

    def test_sum(self):
        self._seed()
        assert User.aggregate("SUM", "age") == 90

    def test_avg(self):
        self._seed()
        assert User.aggregate("AVG", "age") == 30.0

    def test_min(self):
        self._seed()
        assert User.aggregate("MIN", "score") == 80.0

    def test_max(self):
        self._seed()
        assert User.aggregate("MAX", "score") == 100.0

    def test_count_aggregate(self):
        self._seed()
        assert User.aggregate("COUNT", "id") == 3

    def test_filtered_aggregate(self):
        self._seed()
        result = User.filter(age__gte=30).aggregate("SUM", "age")
        assert result == 70  # 30 + 40


# ---------------------------------------------------------------------------
# Bulk Operations Tests
# ---------------------------------------------------------------------------

class TestBulkOps:
    def test_bulk_update(self):
        User.create_table()
        User.create(name="A", email="a@test.com", age=20, active=True)
        User.create(name="B", email="b@test.com", age=30, active=True)
        User.create(name="C", email="c@test.com", age=40, active=True)
        count = User.filter(age__gte=30).update(active=False)
        assert count == 2
        assert User.filter(active=False).count() == 2

    def test_bulk_delete(self):
        User.create_table()
        User.create(name="A", email="a@test.com", age=20)
        User.create(name="B", email="b@test.com", age=30)
        User.create(name="C", email="c@test.com", age=40)
        count = User.filter(age__lt=30).delete()
        assert count == 1
        assert User.count() == 2


# ---------------------------------------------------------------------------
# Foreign Key Tests
# ---------------------------------------------------------------------------

class TestForeignKey:
    def test_fk_insert_and_query(self):
        User.create_table()
        Post.create_table()
        u = User.create(name="Alice", email="alice@test.com")
        p = Post.create(title="Hello", author=u.id)
        fetched = Post.get(id=p.id)
        assert fetched.author == u.id

    def test_fk_cascade_delete(self):
        User.create_table()
        Post.create_table()
        u = User.create(name="Alice", email="alice@test.com")
        Post.create(title="Post1", author=u.id)
        Post.create(title="Post2", author=u.id)
        u.delete()
        assert Post.count() == 0  # CASCADE

    def test_select_related(self):
        User.create_table()
        Post.create_table()
        u = User.create(name="Alice", email="alice@test.com")
        Post.create(title="Hello", author=u.id)
        results = Post.select_related("author").all()
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_table_query(self):
        User.create_table()
        assert User.all() == []
        assert User.count() == 0
        assert not User.exists()

    def test_iteration(self):
        User.create_table()
        User.create(name="A", email="a@test.com")
        User.create(name="B", email="b@test.com")
        names = [u.name for u in User.filter()]
        assert set(names) == {"A", "B"}

    def test_repr(self):
        User.create_table()
        u = User.create(name="Alice", email="alice@test.com")
        assert "User" in repr(u)
        assert str(u.id) in repr(u)

    def test_equality(self):
        User.create_table()
        u1 = User.create(name="Alice", email="alice@test.com")
        u2 = User.get(id=u1.id)
        assert u1 == u2

    def test_delete_unsaved_raises(self):
        User.create_table()
        u = User(name="Test", email="test@test.com")
        with pytest.raises(RecordNotFoundError, match="unsaved"):
            u.delete()

    def test_refresh_unsaved_raises(self):
        User.create_table()
        u = User(name="Test", email="test@test.com")
        with pytest.raises(RecordNotFoundError, match="unsaved"):
            u.refresh()


# ---------------------------------------------------------------------------
# Thread Safety Tests
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_inserts(self):
        """Multiple threads inserting rows concurrently should not lose data."""
        User.create_table()
        errors: list[Exception] = []
        num_threads = 5
        rows_per_thread = 10

        def worker(thread_id: int):
            try:
                for i in range(rows_per_thread):
                    User.create(
                        name=f"Thread{thread_id}_User{i}",
                        email=f"t{thread_id}_u{i}@test.com",
                        age=thread_id * 100 + i,
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        assert User.count() == num_threads * rows_per_thread

    def test_concurrent_reads_and_writes(self):
        """Readers and writers operating concurrently should not crash."""
        User.create_table()
        for i in range(20):
            User.create(name=f"User{i}", email=f"u{i}@test.com", age=i)

        errors: list[Exception] = []

        def reader():
            try:
                for _ in range(10):
                    User.filter(age__gte=5).order_by("name").all()
                    User.count()
            except Exception as exc:
                errors.append(exc)

        def writer(wid: int):
            try:
                for i in range(5):
                    User.create(
                        name=f"W{wid}_{i}",
                        email=f"w{wid}_{i}@test.com",
                        age=1000 + wid * 100 + i,
                    )
            except Exception as exc:
                errors.append(exc)

        threads = (
            [threading.Thread(target=reader) for _ in range(3)]
            + [threading.Thread(target=writer, args=(w,)) for w in range(2)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        # 20 initial + 2 writers Ã— 5 rows each = 30
        assert User.count() == 30



