"""Tests for core ORM: CRUD, dirty tracking, queries, aggregations."""

import pytest
from obele import (
    Database, Model, TextField, IntegerField, RealField, BooleanField,
    DateTimeField, JSONField, ForeignKeyField,
    Q, F, Count, Sum, Avg, Min, Max,
    FieldValidationError, RecordNotFoundError, IntegrityError,
)


class User(Model):
    table_name = "users"
    name = TextField()
    age = IntegerField(nullable=True)
    active = BooleanField(default=True)


class Category(Model):
    table_name = "categories"
    name = TextField(unique=True)


class Product(Model):
    table_name = "products"
    name = TextField()
    price = RealField(default=0.0)
    category = ForeignKeyField(to=Category, related_name="products")
    meta = JSONField(nullable=True)


@pytest.fixture(autouse=True)
def create_tables():
    User.create_table()
    Category.create_table()
    Product.create_table()
    yield


class TestCRUD:
    def test_create_and_read(self):
        u = User.create(name="Alice", age=30)
        assert u.id is not None
        assert u.name == "Alice"
        assert u.age == 30

    def test_save_insert(self):
        u = User(name="Bob", age=25)
        u.save()
        assert u.id is not None
        assert User.count() == 1

    def test_save_update(self):
        u = User.create(name="Carol", age=35)
        u.name = "Carla"
        u.save()
        reloaded = User.get(id=u.id)
        assert reloaded.name == "Carla"

    def test_delete(self):
        u = User.create(name="Dave")
        u.delete()
        assert User.count() == 0

    def test_delete_unsaved_raises(self):
        u = User(name="Nobody")
        with pytest.raises(RecordNotFoundError):
            u.delete()

    def test_refresh(self):
        u = User.create(name="Eve", age=20)
        Database.execute(
            f"UPDATE users SET name = 'Eva' WHERE id = ?", [u.id]
        )
        u.refresh()
        assert u.name == "Eva"

    def test_upsert(self):
        u = User.create(name="Frank", age=40)
        u2 = User.upsert(id=u.id, name="Franklin", age=40)
        assert User.count() == 1
        assert User.get(id=u.id).name == "Franklin"


class TestDirtyTracking:
    def test_clean_after_create(self):
        u = User.create(name="A")
        assert not u.is_dirty
        assert u.dirty_fields == {}

    def test_dirty_after_modification(self):
        u = User.create(name="A")
        u.name = "B"
        assert u.is_dirty
        assert "name" in u.dirty_fields

    def test_clean_after_save(self):
        u = User.create(name="A")
        u.name = "B"
        u.save()
        assert not u.is_dirty


class TestQueries:
    def test_filter_exact(self):
        User.create(name="Alice", age=30)
        User.create(name="Bob", age=25)
        result = User.filter(name="Alice").all()
        assert len(result) == 1
        assert result[0].name == "Alice"

    def test_filter_gte_lte(self):
        User.create(name="A", age=20)
        User.create(name="B", age=30)
        User.create(name="C", age=40)
        result = User.filter(age__gte=25, age__lte=35).all()
        assert len(result) == 1

    def test_filter_contains(self):
        User.create(name="Alice")
        User.create(name="Bob")
        result = User.filter(name__contains="lic").all()
        assert len(result) == 1

    def test_filter_in(self):
        User.create(name="Alice")
        User.create(name="Bob")
        User.create(name="Carol")
        result = User.filter(name__in=["Alice", "Carol"]).all()
        assert len(result) == 2

    def test_q_or(self):
        User.create(name="Alice", age=30)
        User.create(name="Bob", age=25)
        User.create(name="Carol", age=35)
        result = User.filter(Q(name="Alice") | Q(age=35)).all()
        assert len(result) == 2

    def test_q_not(self):
        User.create(name="Alice")
        User.create(name="Bob")
        result = User.filter(~Q(name="Alice")).all()
        assert len(result) == 1
        assert result[0].name == "Bob"

    def test_exclude(self):
        User.create(name="Alice")
        User.create(name="Bob")
        result = User.exclude(name="Alice").all()
        assert len(result) == 1

    def test_order_by(self):
        User.create(name="Carol")
        User.create(name="Alice")
        User.create(name="Bob")
        names = [u.name for u in User.order_by("name").all()]
        assert names == ["Alice", "Bob", "Carol"]

    def test_order_by_desc(self):
        User.create(name="A", age=1)
        User.create(name="B", age=2)
        ages = [u.age for u in User.order_by("-age").all()]
        assert ages == [2, 1]

    def test_limit_offset(self):
        for i in range(10):
            User.create(name=f"u{i}", age=i)
        result = User.order_by("age").offset(3).limit(3).all()
        assert len(result) == 3
        assert result[0].age == 3

    def test_count(self):
        User.create(name="A")
        User.create(name="B")
        assert User.count() == 2

    def test_exists(self):
        assert not User.exists()
        User.create(name="A")
        assert User.exists()

    def test_first_empty(self):
        assert User.first() is None

    def test_get_not_found(self):
        with pytest.raises(RecordNotFoundError):
            User.get(name="nope")

    def test_values(self):
        User.create(name="Alice", age=30)
        result = User.values("name", "age").all()
        assert result[0] == {"name": "Alice", "age": 30}

    def test_values_list_flat(self):
        User.create(name="Alice")
        User.create(name="Bob")
        names = User.order_by("name").values_list("name", flat=True).all()
        assert names == ["Alice", "Bob"]

    def test_distinct(self):
        User.create(name="A", age=10)
        User.create(name="A", age=20)
        result = User.distinct().values_list("name", flat=True).all()
        assert len(result) == 1


class TestAggregation:
    def test_sum(self):
        User.create(name="A", age=10)
        User.create(name="B", age=20)
        assert User.aggregate("SUM", "age") == 30

    def test_avg(self):
        User.create(name="A", age=10)
        User.create(name="B", age=20)
        assert User.aggregate("AVG", "age") == 15.0

    def test_min_max(self):
        User.create(name="A", age=10)
        User.create(name="B", age=20)
        assert User.aggregate("MIN", "age") == 10
        assert User.aggregate("MAX", "age") == 20

    def test_count_aggregate(self):
        User.create(name="A")
        User.create(name="B")
        assert User.aggregate("COUNT", "id") == 2


class TestForeignKeys:
    def test_fk_create_and_read(self):
        cat = Category.create(name="Electronics")
        prod = Product.create(name="Phone", price=999, category=cat)
        prod.refresh()
        assert prod.category == cat.id

    def test_select_related(self):
        cat = Category.create(name="Books")
        Product.create(name="Novel", price=15, category=cat)
        prod = Product.select_related("category").first()
        assert prod.category.name == "Books"

    def test_reverse_relation(self):
        cat = Category.create(name="Toys")
        Product.create(name="Ball", price=5, category=cat)
        Product.create(name="Doll", price=10, category=cat)
        products = cat.products.all()
        assert len(products) == 2

    def test_filter_across_fk(self):
        c1 = Category.create(name="A")
        c2 = Category.create(name="B")
        Product.create(name="P1", price=10, category=c1)
        Product.create(name="P2", price=20, category=c2)
        result = Product.filter(category__name="A").all()
        assert len(result) == 1
        assert result[0].name == "P1"


class TestBulkOperations:
    def test_bulk_create(self):
        items = [{"name": f"user{i}", "age": i * 10} for i in range(5)]
        users = User.bulk_create(items)
        assert len(users) == 5
        assert all(u.id is not None for u in users)
        assert User.count() == 5

    def test_bulk_update(self):
        users = User.bulk_create([
            {"name": "A", "age": 10},
            {"name": "B", "age": 20},
        ])
        for u in users:
            u.age = u.age + 100
        affected = User.bulk_update(users, fields=["age"])
        assert affected == 2
        assert User.get(id=users[0].id).age == 110

    def test_get_or_create(self):
        user, created = User.get_or_create(name="Alice", defaults={"age": 30})
        assert created is True
        user2, created2 = User.get_or_create(name="Alice", defaults={"age": 99})
        assert created2 is False
        assert user2.age == 30

    def test_update_or_create(self):
        user, created = User.update_or_create(
            defaults={"age": 30}, name="Alice"
        )
        assert created is True
        user2, created2 = User.update_or_create(
            defaults={"age": 40}, name="Alice"
        )
        assert created2 is False
        assert user2.age == 40


class TestTransactions:
    def test_transaction_commit(self):
        with Database.transaction():
            User.create(name="A")
            User.create(name="B")
        assert User.count() == 2

    def test_transaction_rollback(self):
        try:
            with Database.transaction():
                User.create(name="A")
                raise ValueError("rollback")
        except ValueError:
            pass
        assert User.count() == 0
