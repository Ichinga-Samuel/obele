"""Tests for features introduced in the 0.2 rewrite."""

import datetime

import pytest

import obele
from obele import (
    Database, Model, TextField, IntegerField, DateTimeField, ForeignKeyField,
    KVStore, F, Value, Count, Q,
    FieldValidationError, RecordNotFoundError,
    create_all, drop_all, registered_models,
)


class Author(Model):
    table_name = "authors_v2"
    name = TextField()
    score = IntegerField(default=0)


class Book(Model):
    table_name = "books_v2"
    title = TextField()
    views = IntegerField(default=0)
    author = ForeignKeyField(to=Author, related_name="books")


@pytest.fixture(autouse=True)
def tables():
    create_all([Author, Book])
    yield


class TestPkProperty:
    def test_pk_reads_and_writes(self):
        a = Author.create(name="A")
        assert a.pk == a.id
        a.pk = 99
        assert a.id == 99

    def test_pk_none_before_save(self):
        assert Author(name="x").pk is None


class TestQuerySetProxy:
    def test_class_level_queryset_methods(self):
        Author.create(name="A", score=1)
        Author.create(name="B", score=2)
        assert Author.count() == 2
        assert Author.order_by("-score").first().name == "B"
        assert Author.exists() is True
        assert Author.values_list("name", flat=True) is not None

    def test_missing_attribute_still_raises(self):
        with pytest.raises(AttributeError):
            Author.not_a_real_method


class TestSlicing:
    def test_slice_is_lazy_and_limits(self):
        for i in range(10):
            Author.create(name=f"a{i}", score=i)
        qs = Author.order_by("score")[2:5]
        assert [a.score for a in qs.all()] == [2, 3, 4]

    def test_index_returns_instance(self):
        for i in range(3):
            Author.create(name=f"a{i}", score=i)
        assert Author.order_by("score")[1].score == 1

    def test_index_out_of_range(self):
        with pytest.raises(IndexError):
            Author.order_by("score")[5]

    def test_negative_index_rejected(self):
        with pytest.raises(ValueError):
            Author.order_by("score")[-1]


class TestFArithmetic:
    def test_update_with_expression(self):
        a = Author.create(name="A")
        b = Book.create(title="t", views=7, author=a)
        Book.filter(id=b.pk).update(views=F("views") + 10)
        assert Book.get_by_pk(b.pk).views == 17

    def test_combined_expression_in_filter(self):
        a = Author.create(name="A", score=10)
        Author.create(name="B", score=3)
        found = Author.filter(score__gte=F("score") * 0 + 10).all()
        assert [x.name for x in found] == ["A"]

    def test_annotate_arithmetic(self):
        Author.create(name="A", score=5)
        row = Author.annotate(double=F("score") * 2).first()
        assert row._annotations["double"] == 10


class TestCountStar:
    def test_count_with_no_args(self):
        a = Author.create(name="A")
        Book.create(title="x", author=a)
        Book.create(title="y", author=a)
        result = Author.annotate(n=Count()).group_by("id").first()
        assert result._annotations["n"] == 1

    def test_count_field_name_coerces_to_column(self):
        a = Author.create(name="A")
        Book.create(title="x", author=a)
        row = Author.annotate(n=Count("books__id")).first()
        assert row._annotations["n"] == 1


class TestTerminalHelpers:
    def test_in_bulk(self):
        a = Author.create(name="A")
        b = Author.create(name="B")
        mapping = Author.in_bulk([a.pk, b.pk])
        assert mapping[a.pk].name == "A" and mapping[b.pk].name == "B"

    def test_in_bulk_by_field(self):
        Author.create(name="A")
        mapping = Author.in_bulk(["A"], field="name")
        assert mapping["A"].name == "A"

    def test_latest_earliest(self):
        Author.create(name="old", score=1)
        Author.create(name="new", score=9)
        assert Author.latest("score").name == "new"
        assert Author.earliest("score").name == "old"

    def test_latest_raises_when_empty(self):
        with pytest.raises(RecordNotFoundError):
            Author.latest()

    def test_last_reverses_ordering(self):
        for i in range(3):
            Author.create(name=f"a{i}", score=i)
        assert Author.order_by("score").last().score == 2
        assert Author.last().pk == Author.latest().pk


class TestPrefetchCache:
    def test_prefetched_manager_serves_cached_rows(self):
        a = Author.create(name="A")
        Book.create(title="one", author=a)
        Book.create(title="two", author=a)
        (author,) = Author.filter(id=a.pk).prefetch_related("books").all()

        calls: list[str] = []
        original = Database.execute_read.__func__

        def spy(cls, sql, params=None):
            calls.append(sql)
            return original(cls, sql, params)

        Database.execute_read = classmethod(spy)
        try:
            titles = sorted(b.title for b in author.books.all())
            count = author.books.count()
        finally:
            Database.execute_read = classmethod(original)

        assert titles == ["one", "two"]
        assert count == 2
        assert calls == []  # served entirely from the prefetch cache

    def test_unprefetched_manager_still_queries(self):
        a = Author.create(name="A")
        Book.create(title="one", author=a)
        assert a.books.count() == 1


class TestRegexLookup:
    def test_regex_filter(self):
        Author.create(name="Alice")
        Author.create(name="Bob")
        names = [a.name for a in Author.filter(name__regex=r"^A")]
        assert names == ["Alice"]


class TestSetOperationGuards:
    def test_union_supports_order_and_limit(self):
        Author.create(name="A", score=1)
        Author.create(name="B", score=2)
        combined = Author.filter(score=1).union(Author.filter(score=2))
        results = combined.order_by("score").limit(1).all()
        assert [r.score for r in results] == [1]

    def test_filter_after_union_raises(self):
        combined = Author.filter(score=1).union(Author.filter(score=2))
        with pytest.raises(ValueError):
            combined.filter(name="A")


class TestOnlyDeferValidation:
    def test_only_unknown_field_raises(self):
        with pytest.raises(ValueError):
            Author.only("nope")

    def test_defer_unknown_field_raises(self):
        with pytest.raises(ValueError):
            Author.defer("nope")


class TestFieldEnhancements:
    def test_choices_validation(self):
        class Ranked(Model):
            table_name = "ranked_v2"
            level = TextField(choices=("low", "high"))

        Ranked.create_table()
        Ranked.create(level="low")
        with pytest.raises(FieldValidationError):
            Ranked.create(level="medium")

    def test_integer_bounds(self):
        class Bounded(Model):
            table_name = "bounded_v2"
            age = IntegerField(min_value=0, max_value=120)

        Bounded.create_table()
        Bounded.create(age=30)
        with pytest.raises(FieldValidationError):
            Bounded.create(age=-1)
        with pytest.raises(FieldValidationError):
            Bounded.create(age=200)

    def test_datetime_field_has_no_implicit_default(self):
        class Stamped(Model):
            table_name = "stamped_v2"
            at = DateTimeField(nullable=True)

        Stamped.create_table()
        item = Stamped.create()
        assert item.at is None  # previously silently defaulted to now()

    def test_fk_on_delete_validated(self):
        with pytest.raises(ValueError):
            ForeignKeyField(to=Author, on_delete="EXPLODE")


class TestSchemaHelpers:
    def test_registered_models_orders_fk_targets_first(self):
        ordered = registered_models()
        assert ordered.index(Author) < ordered.index(Book)

    def test_create_all_and_drop_all(self):
        drop_all([Book, Author])
        assert "authors_v2" not in Database.tables()
        create_all([Author, Book])
        assert "authors_v2" in Database.tables()
        assert "books_v2" in Database.tables()


class TestBulkCreateOrder:
    def test_mixed_shapes_preserve_input_order(self):
        a = Author.create(name="A")
        items = [
            {"title": "t0", "author": a.pk, "views": 5},
            {"title": "t1", "author": a.pk},  # different column signature
            {"title": "t2", "author": a.pk, "views": 7},
        ]
        created = Book.bulk_create(items)
        assert [b.title for b in created] == ["t0", "t1", "t2"]
        assert created[1].views == 0  # default applied

    def test_large_batch_chunks(self):
        a = Author.create(name="A")
        created = Book.bulk_create([{"title": f"t{i}", "author": a.pk} for i in range(500)])
        assert len(created) == 500
        assert Book.count() == 500
        assert created[0].pk is not None


class TestAsyncParity:
    async def test_async_model_flow(self):
        author = await Author.acreate(name="A")
        assert (await Author.aget(name="A")).pk == author.pk
        assert await Author.acount() == 1
        await Book.abulk_create([{"title": f"b{i}", "author": author.pk} for i in range(3)])
        assert await Book.acount() == 3
        assert (await Book.alatest()).title == "b2"
        assert set((await Book.ain_bulk([1, 2])).keys()) == {1, 2}

    async def test_async_transaction_with_model_helpers(self):
        async with Database.transaction():
            author, created = await Author.aget_or_create(name="tx-author")
            assert created is True
            # nested helper reuses the pinned transaction connection
            again, created = await Author.aget_or_create(name="tx-author")
            assert created is False and again.pk == author.pk
        assert await Author.acount() == 1

    async def test_aiterator_streams(self):
        for i in range(5):
            await Author.acreate(name=f"s{i}", score=i)
        seen = [a.score async for a in Author.order_by("score").aiterator(chunk_size=2)]
        assert seen == [0, 1, 2, 3, 4]


class TestKVEnhancements:
    def test_prefix_empty_returns_all_string_keys(self):
        kv = KVStore("kv_v2", key_type=str)
        kv.update({"a": 1, "b": 2})
        assert kv.prefix("") == {"a": 1, "b": 2}
        assert kv.prefix_count("") == 2

    def test_memoize_sync(self):
        kv = KVStore("kv_memo_v2", key_type=str)
        calls: list[int] = []

        @kv.memoize(ttl=60)
        def double(x: int) -> int:
            calls.append(x)
            return x * 2

        assert double(4) == 8
        assert double(4) == 8
        assert calls == [4]

    async def test_memoize_async(self):
        kv = KVStore("kv_amemo_v2", key_type=str)
        calls: list[int] = []

        @kv.memoize(ttl=60)
        async def triple(x: int) -> int:
            calls.append(x)
            return x * 3

        assert await triple(3) == 9
        assert await triple(3) == 9
        assert calls == [3]

    def test_increment_starts_from_zero(self):
        kv = KVStore("kv_incr_v2", key_type=str)
        assert kv.increment("hits") == 1
        assert kv.increment("hits", 4) == 5

    def test_pop_returns_stored_none(self):
        kv = KVStore("kv_pop_v2", key_type=str)
        kv["k"] = None
        assert kv.pop("k") is None
        assert "k" not in kv

    async def test_apop_returns_stored_none(self):
        kv = KVStore("kv_apop_v2", key_type=str)
        await kv.aset("k", None)
        assert await kv.apop("k") is None
        assert not await kv.acontains("k")
