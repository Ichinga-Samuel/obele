"""Comprehensive tests for all async APIs in the obele library.

Covers:
- Model async CRUD: acreate, asave, adelete, arefresh, acreate_table, adrop_table
- Model async queries: aall, aget, afirst, acount, aexists, aaggregate
- QuerySet async: aall, afirst, aget, acount, aexists, aupdate, adelete,
                  aaggregate, apaginate, acursor_paginate, aiterator
- TimestampMixin: asave
- SoftDeleteMixin: adelete, ahard_delete, arestore
- ReverseRelationManager: acreate, aall, acount
- SearchIndex: acreate, adrop, arebuild, aoptimize, asearch, asearch_count
- KVStore: full async dict-like API, TTL, atomic ops, prefix, scan, stats
"""

import datetime

import pytest

from obele import (
    Database,
    Model,
    TextField,
    IntegerField,
    ForeignKeyField,
    BooleanField,
    TimestampMixin,
    SoftDeleteMixin,
    SearchIndex,
    KVStore,
    Page,
    CursorPage,
    RecordNotFoundError,
)


# ---------------------------------------------------------------------------
# Model definitions
# ---------------------------------------------------------------------------

class AsyncUser(Model):
    table_name = "async_users"
    name = TextField()
    age = IntegerField(nullable=True)


class AsyncPost(Model):
    table_name = "async_posts"
    title = TextField()
    author = ForeignKeyField(to=AsyncUser, related_name="posts")


class AsyncArticle(TimestampMixin, SoftDeleteMixin, Model):
    table_name = "async_articles"
    title = TextField()


class AsyncDoc(Model):
    table_name = "async_docs"
    title = TextField()
    body = TextField()


# ===================================================================
# Model async CRUD
# ===================================================================

class TestAsyncModelCRUD:
    @pytest.fixture(autouse=True)
    def setup(self):
        AsyncUser.create_table()
        yield

    # -- acreate --

    async def test_acreate_returns_instance(self):
        user = await AsyncUser.acreate(name="Alice", age=30)
        assert user.id is not None
        assert user.name == "Alice"
        assert user.age == 30

    async def test_acreate_with_defaults(self):
        user = await AsyncUser.acreate(name="Bob")
        assert user.name == "Bob"
        assert user.age is None

    async def test_acreate_persists_to_db(self):
        await AsyncUser.acreate(name="Carol", age=25)
        assert AsyncUser.count() == 1

    # -- asave (insert) --

    async def test_asave_insert(self):
        user = AsyncUser(name="Dave", age=40)
        await user.asave()
        assert user.id is not None
        assert AsyncUser.count() == 1

    # -- asave (update) --

    async def test_asave_update(self):
        user = await AsyncUser.acreate(name="Eve", age=20)
        user.name = "Eva"
        await user.asave()
        reloaded = AsyncUser.get(id=user.id)
        assert reloaded.name == "Eva"

    async def test_asave_update_only_dirty_fields(self):
        user = await AsyncUser.acreate(name="Frank", age=50)
        user.age = 51
        await user.asave()
        reloaded = AsyncUser.get(id=user.id)
        assert reloaded.age == 51
        assert reloaded.name == "Frank"

    # -- adelete --

    async def test_adelete(self):
        user = await AsyncUser.acreate(name="Grace")
        await user.adelete()
        assert await AsyncUser.acount() == 0

    async def test_adelete_unsaved_raises(self):
        user = AsyncUser(name="Nobody")
        with pytest.raises(RecordNotFoundError):
            await user.adelete()

    # -- arefresh --

    async def test_arefresh(self):
        user = await AsyncUser.acreate(name="Hank", age=30)
        Database.execute(
            "UPDATE async_users SET name = 'Henry' WHERE id = ?", [user.id]
        )
        await user.arefresh()
        assert user.name == "Henry"

    async def test_arefresh_unsaved_raises(self):
        user = AsyncUser(name="Nobody")
        with pytest.raises(RecordNotFoundError):
            await user.arefresh()


# ===================================================================
# Model async table management
# ===================================================================

class TestAsyncTableManagement:

    async def test_acreate_table_and_adrop_table(self):
        await AsyncUser.acreate_table()
        await AsyncUser.acreate(name="Test")
        assert await AsyncUser.acount() == 1
        await AsyncUser.adrop_table()
        # Re-create so teardown is clean
        await AsyncUser.acreate_table()

    async def test_acreate_table_idempotent(self):
        await AsyncUser.acreate_table()
        await AsyncUser.acreate_table()  # should not raise


# ===================================================================
# Model async class-level queries
# ===================================================================

class TestAsyncModelQueries:
    @pytest.fixture(autouse=True)
    def setup(self):
        AsyncUser.create_table()
        yield

    async def test_aall_empty(self):
        result = await AsyncUser.aall()
        assert result == []

    async def test_aall_returns_all(self):
        AsyncUser.create(name="A", age=10)
        AsyncUser.create(name="B", age=20)
        result = await AsyncUser.aall()
        assert len(result) == 2

    async def test_afirst_empty(self):
        result = await AsyncUser.afirst()
        assert result is None

    async def test_afirst_returns_one(self):
        AsyncUser.create(name="Alice", age=30)
        AsyncUser.create(name="Bob", age=25)
        result = await AsyncUser.afirst()
        assert result is not None
        assert result.name in ("Alice", "Bob")

    async def test_aget_found(self):
        user = AsyncUser.create(name="Charlie", age=35)
        result = await AsyncUser.aget(id=user.id)
        assert result.name == "Charlie"

    async def test_aget_not_found_raises(self):
        with pytest.raises(RecordNotFoundError):
            await AsyncUser.aget(name="nonexistent")

    async def test_acount(self):
        AsyncUser.create(name="A")
        AsyncUser.create(name="B")
        AsyncUser.create(name="C")
        assert await AsyncUser.acount() == 3

    async def test_acount_empty(self):
        assert await AsyncUser.acount() == 0

    async def test_aexists_true(self):
        AsyncUser.create(name="X")
        assert await AsyncUser.aexists() is True

    async def test_aexists_false(self):
        assert await AsyncUser.aexists() is False

    async def test_aaggregate_sum(self):
        AsyncUser.create(name="A", age=10)
        AsyncUser.create(name="B", age=20)
        result = await AsyncUser.aaggregate("SUM", "age")
        assert result == 30

    async def test_aaggregate_avg(self):
        AsyncUser.create(name="A", age=10)
        AsyncUser.create(name="B", age=30)
        result = await AsyncUser.aaggregate("AVG", "age")
        assert result == 20.0

    async def test_aaggregate_min_max(self):
        AsyncUser.create(name="A", age=5)
        AsyncUser.create(name="B", age=50)
        assert await AsyncUser.aaggregate("MIN", "age") == 5
        assert await AsyncUser.aaggregate("MAX", "age") == 50


# ===================================================================
# QuerySet async methods
# ===================================================================

class TestAsyncQuerySet:
    @pytest.fixture(autouse=True)
    def setup(self):
        AsyncUser.create_table()
        yield

    async def test_qs_aall(self):
        AsyncUser.create(name="Alice", age=30)
        AsyncUser.create(name="Bob", age=25)
        result = await AsyncUser.filter(age__gte=25).aall()
        assert len(result) == 2

    async def test_qs_aall_filtered(self):
        AsyncUser.create(name="Alice", age=30)
        AsyncUser.create(name="Bob", age=20)
        result = await AsyncUser.filter(age__gt=25).aall()
        assert len(result) == 1
        assert result[0].name == "Alice"

    async def test_qs_afirst(self):
        AsyncUser.create(name="Alice", age=30)
        result = await AsyncUser.filter(name="Alice").afirst()
        assert result is not None
        assert result.name == "Alice"

    async def test_qs_afirst_no_match(self):
        result = await AsyncUser.filter(name="Nobody").afirst()
        assert result is None

    async def test_qs_aget(self):
        user = AsyncUser.create(name="Carol", age=35)
        result = await AsyncUser.filter(age=35).aget(name="Carol")
        assert result.id == user.id

    async def test_qs_aget_not_found(self):
        with pytest.raises(RecordNotFoundError):
            await AsyncUser.filter(name="Nobody").aget(name="Nobody")

    async def test_qs_acount(self):
        AsyncUser.create(name="A", age=10)
        AsyncUser.create(name="B", age=20)
        AsyncUser.create(name="C", age=30)
        assert await AsyncUser.filter(age__gte=20).acount() == 2

    async def test_qs_aexists(self):
        AsyncUser.create(name="A")
        assert await AsyncUser.filter(name="A").aexists() is True
        assert await AsyncUser.filter(name="Z").aexists() is False

    async def test_qs_aupdate(self):
        AsyncUser.create(name="Alice", age=30)
        AsyncUser.create(name="Bob", age=25)
        affected = await AsyncUser.filter(name="Alice").aupdate(age=31)
        assert affected == 1
        assert AsyncUser.get(name="Alice").age == 31

    async def test_qs_aupdate_multiple(self):
        AsyncUser.create(name="A", age=10)
        AsyncUser.create(name="B", age=10)
        AsyncUser.create(name="C", age=20)
        affected = await AsyncUser.filter(age=10).aupdate(age=99)
        assert affected == 2

    async def test_qs_adelete(self):
        AsyncUser.create(name="Alice")
        AsyncUser.create(name="Bob")
        deleted = await AsyncUser.filter(name="Alice").adelete()
        assert deleted == 1
        assert AsyncUser.count() == 1

    async def test_qs_adelete_multiple(self):
        AsyncUser.create(name="A", age=10)
        AsyncUser.create(name="B", age=10)
        AsyncUser.create(name="C", age=20)
        deleted = await AsyncUser.filter(age=10).adelete()
        assert deleted == 2
        assert AsyncUser.count() == 1

    async def test_qs_aaggregate(self):
        AsyncUser.create(name="A", age=10)
        AsyncUser.create(name="B", age=20)
        AsyncUser.create(name="C", age=30)
        result = await AsyncUser.filter(age__gte=20).aaggregate("SUM", "age")
        assert result == 50

    async def test_qs_aaggregate_count(self):
        AsyncUser.create(name="A", age=10)
        AsyncUser.create(name="B", age=20)
        result = await AsyncUser.filter(age__gte=10).aaggregate("COUNT", "id")
        assert result == 2


# ===================================================================
# QuerySet async pagination
# ===================================================================

class TestAsyncPagination:
    @pytest.fixture(autouse=True)
    def setup(self):
        AsyncUser.create_table()
        AsyncUser.bulk_create([{"name": f"user{i}", "age": i} for i in range(50)])
        yield

    async def test_apaginate_basic(self):
        page = await AsyncUser.order_by("age").apaginate(page=1, per_page=10)
        assert isinstance(page, Page)
        assert len(page.items) == 10
        assert page.total == 50
        assert page.pages == 5

    async def test_apaginate_second_page(self):
        page = await AsyncUser.order_by("age").apaginate(page=2, per_page=10)
        assert len(page.items) == 10
        assert page.items[0].age == 10

    async def test_apaginate_last_page(self):
        page = await AsyncUser.order_by("age").apaginate(page=5, per_page=10)
        assert len(page.items) == 10
        assert page.items[-1].age == 49

    async def test_acursor_paginate_basic(self):
        page = await AsyncUser.order_by("id").acursor_paginate(per_page=10)
        assert isinstance(page, CursorPage)
        assert len(page.items) == 10
        assert page.has_next is True

    async def test_acursor_paginate_forward(self):
        p1 = await AsyncUser.order_by("id").acursor_paginate(per_page=10)
        p2 = await AsyncUser.order_by("id").acursor_paginate(
            per_page=10, after=p1.end_cursor
        )
        assert len(p2.items) == 10
        assert p2.items[0].id > p1.items[-1].id


# ===================================================================
# QuerySet async iterator
# ===================================================================

class TestAsyncIterator:
    @pytest.fixture(autouse=True)
    def setup(self):
        AsyncUser.create_table()
        AsyncUser.bulk_create([{"name": f"u{i}", "age": i} for i in range(25)])
        yield

    async def test_aiterator_yields_all(self):
        items = []
        async for user in AsyncUser.order_by("age").aiterator(chunk_size=5):
            items.append(user)
        assert len(items) == 25

    async def test_aiterator_order(self):
        ages = []
        async for user in AsyncUser.order_by("age").aiterator(chunk_size=10):
            ages.append(user.age)
        assert ages == list(range(25))

    async def test_aiterator_with_filter(self):
        items = []
        async for user in AsyncUser.filter(age__gte=20).order_by("age").aiterator(chunk_size=3):
            items.append(user)
        assert len(items) == 5
        assert items[0].age == 20


# ===================================================================
# TimestampMixin async
# ===================================================================

class TestAsyncTimestampMixin:
    @pytest.fixture(autouse=True)
    def setup(self):
        AsyncArticle.create_table()
        yield

    async def test_asave_sets_timestamps_on_insert(self):
        article = AsyncArticle(title="Async Article")
        await article.asave()
        assert article.id is not None
        assert isinstance(article.created_at, datetime.datetime)
        assert isinstance(article.updated_at, datetime.datetime)

    async def test_asave_updates_updated_at(self):
        article = AsyncArticle(title="Original")
        await article.asave()
        original_updated = article.updated_at
        article.title = "Modified"
        await article.asave()
        assert article.updated_at >= original_updated


# ===================================================================
# SoftDeleteMixin async
# ===================================================================

class TestAsyncSoftDeleteMixin:
    @pytest.fixture(autouse=True)
    def setup(self):
        AsyncArticle.create_table()
        yield

    async def test_adelete_soft_deletes(self):
        article = AsyncArticle.create(title="To Delete")
        await article.adelete()
        assert article.is_deleted is True
        assert article.deleted_at is not None

    async def test_adelete_excludes_from_default_queries(self):
        AsyncArticle.create(title="Keep")
        art = AsyncArticle.create(title="Remove")
        await art.adelete()
        assert AsyncArticle.count() == 1

    async def test_arestore(self):
        article = AsyncArticle.create(title="Restore Me")
        await article.adelete()
        assert article.is_deleted is True
        await article.arestore()
        assert article.is_deleted is False
        assert article.deleted_at is None
        assert AsyncArticle.count() == 1

    async def test_ahard_delete(self):
        article = AsyncArticle.create(title="Hard Delete")
        await article.ahard_delete()
        assert AsyncArticle.with_deleted().count() == 0

    async def test_ahard_delete_is_permanent(self):
        AsyncArticle.create(title="A")
        art = AsyncArticle.create(title="B")
        await art.ahard_delete()
        assert AsyncArticle.with_deleted().count() == 1


# ===================================================================
# ReverseRelationManager async
# ===================================================================

class TestAsyncReverseRelation:
    @pytest.fixture(autouse=True)
    def setup(self):
        AsyncUser.create_table()
        AsyncPost.create_table()
        yield

    async def test_acreate_related(self):
        user = AsyncUser.create(name="Alice", age=30)
        post = await user.posts.acreate(title="Hello")
        assert post.id is not None
        assert post.author.id == user.id

    async def test_aall_related(self):
        user = AsyncUser.create(name="Bob")
        AsyncPost.create(title="Post1", author=user)
        AsyncPost.create(title="Post2", author=user)
        posts = await user.posts.aall()
        assert len(posts) == 2

    async def test_acount_related(self):
        user = AsyncUser.create(name="Carol")
        AsyncPost.create(title="P1", author=user)
        AsyncPost.create(title="P2", author=user)
        AsyncPost.create(title="P3", author=user)
        count = await user.posts.acount()
        assert count == 3

    async def test_aall_related_empty(self):
        user = AsyncUser.create(name="Dave")
        posts = await user.posts.aall()
        assert posts == []

    async def test_acount_related_zero(self):
        user = AsyncUser.create(name="Eve")
        assert await user.posts.acount() == 0


# ===================================================================
# SearchIndex async
# ===================================================================

class TestAsyncSearchIndex:
    @pytest.fixture(autouse=True)
    def setup(self):
        AsyncDoc.create_table()
        self.idx = SearchIndex(AsyncDoc, fields=["title", "body"])
        self.idx.create()
        yield
        self.idx.drop()

    async def test_acreate_and_adrop(self):
        idx2 = SearchIndex(AsyncDoc, fields=["title"], fts_table="async_docs_fts2")
        await idx2.acreate()
        await idx2.adrop()

    async def test_arebuild(self):
        AsyncDoc.create(title="Python Guide", body="Learn Python programming")
        AsyncDoc.create(title="SQLite Tips", body="WAL mode for speed")
        await self.idx.arebuild()
        results = self.idx.search("python")
        assert len(results) >= 1

    async def test_aoptimize(self):
        AsyncDoc.create(title="Optimize Test", body="Some content here")
        self.idx.rebuild()
        await self.idx.aoptimize()
        # Should not raise; optimization is silent

    async def test_asearch(self):
        AsyncDoc.create(title="Async IO", body="asyncio is powerful")
        AsyncDoc.create(title="Sync IO", body="blocking calls")
        self.idx.rebuild()
        results = await self.idx.asearch("async")
        assert len(results) >= 1
        assert any("Async" in r.title for r in results)

    async def test_asearch_empty_query(self):
        results = await self.idx.asearch("")
        assert results == []

    async def test_asearch_no_match(self):
        AsyncDoc.create(title="Hello", body="World")
        self.idx.rebuild()
        results = await self.idx.asearch("xyznonexistent")
        assert results == []

    async def test_asearch_with_limit(self):
        for i in range(10):
            AsyncDoc.create(title=f"Python doc {i}", body="Python content")
        self.idx.rebuild()
        results = await self.idx.asearch("python", limit=3)
        assert len(results) == 3

    async def test_asearch_count(self):
        AsyncDoc.create(title="Alpha", body="alpha content")
        AsyncDoc.create(title="Beta", body="beta content")
        AsyncDoc.create(title="Alpha Beta", body="mixed")
        self.idx.rebuild()
        count = await self.idx.asearch_count("alpha")
        assert count >= 1

    async def test_asearch_count_empty_query(self):
        assert await self.idx.asearch_count("") == 0


# ===================================================================
# KVStore async — basic operations
# ===================================================================

class TestAsyncKVBasic:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.store = KVStore("async_kv_basic", key_type=str)
        yield
        self.store.clear()

    async def test_aset_aget(self):
        await self.store.aset("key1", "value1")
        result = await self.store.aget("key1")
        assert result == "value1"

    async def test_aget_default(self):
        result = await self.store.aget("missing", "fallback")
        assert result == "fallback"

    async def test_aget_default_none(self):
        result = await self.store.aget("missing")
        assert result is None

    async def test_adelete(self):
        await self.store.aset("key", "val")
        await self.store.adelete("key")
        assert await self.store.aget("key") is None

    async def test_alen(self):
        await self.store.aset("a", 1)
        await self.store.aset("b", 2)
        assert await self.store.alen() == 2

    async def test_acontains_true(self):
        await self.store.aset("x", 100)
        assert await self.store.acontains("x") is True

    async def test_acontains_false(self):
        assert await self.store.acontains("missing") is False

    async def test_aclear(self):
        await self.store.aset("a", 1)
        await self.store.aset("b", 2)
        await self.store.aclear()
        assert await self.store.alen() == 0


# ===================================================================
# KVStore async — bulk operations
# ===================================================================

class TestAsyncKVBulk:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.store = KVStore("async_kv_bulk", key_type=str)
        yield
        self.store.clear()

    async def test_aset_many_aget_many(self):
        await self.store.aset_many({"k1": "v1", "k2": "v2", "k3": "v3"})
        result = await self.store.aget_many("k1", "k2", "k3")
        assert result["k1"] == "v1"
        assert result["k2"] == "v2"
        assert result["k3"] == "v3"

    async def test_adelete_many(self):
        await self.store.aset_many({"a": 1, "b": 2, "c": 3})
        deleted = await self.store.adelete_many(["a", "b"])
        assert deleted == 2
        assert await self.store.alen() == 1

    async def test_akeys(self):
        await self.store.aset_many({"x": 1, "y": 2})
        keys = await self.store.akeys()
        assert set(keys) == {"x", "y"}

    async def test_avalues(self):
        await self.store.aset_many({"a": 10, "b": 20})
        values = await self.store.avalues()
        assert set(values) == {10, 20}

    async def test_aitems(self):
        await self.store.aset_many({"p": 100, "q": 200})
        items = await self.store.aitems()
        items_dict = dict(items)
        assert items_dict["p"] == 100
        assert items_dict["q"] == 200


# ===================================================================
# KVStore async — atomic operations
# ===================================================================

class TestAsyncKVAtomic:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.store = KVStore("async_kv_atomic", key_type=str)
        yield
        self.store.clear()

    async def test_aincrement(self):
        self.store["counter"] = 0
        result = await self.store.aincrement("counter", 5)
        assert result == 5
        assert self.store["counter"] == 5

    async def test_aincrement_multiple(self):
        self.store["counter"] = 10
        await self.store.aincrement("counter", 3)
        await self.store.aincrement("counter", 7)
        assert self.store["counter"] == 20

    async def test_acompare_and_swap_success(self):
        self.store["val"] = 10
        result = await self.store.acompare_and_swap("val", 10, 20)
        assert result is True
        assert self.store["val"] == 20

    async def test_acompare_and_swap_failure(self):
        self.store["val"] = 10
        result = await self.store.acompare_and_swap("val", 99, 20)
        assert result is False
        assert self.store["val"] == 10


# ===================================================================
# KVStore async — dict-like helpers
# ===================================================================

class TestAsyncKVDictLike:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.store = KVStore("async_kv_dict", key_type=str)
        yield
        self.store.clear()

    async def test_apop_existing(self):
        self.store["key"] = "val"
        result = await self.store.apop("key")
        assert result == "val"
        assert "key" not in self.store

    async def test_apop_missing_with_default(self):
        result = await self.store.apop("missing", "default")
        assert result == "default"

    async def test_apopitem(self):
        self.store["a"] = 1
        self.store["b"] = 2
        key, val = await self.store.apopitem()
        assert key in ("a", "b")
        assert val in (1, 2)

    async def test_asetdefault_missing(self):
        result = await self.store.asetdefault("new_key", 42)
        assert result == 42
        assert self.store["new_key"] == 42

    async def test_asetdefault_existing(self):
        self.store["existing"] = 99
        result = await self.store.asetdefault("existing", 0)
        assert result == 99


# ===================================================================
# KVStore async — prefix and scan
# ===================================================================

class TestAsyncKVPrefixScan:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.store = KVStore("async_kv_prefix", key_type=str)
        yield
        self.store.clear()

    async def test_aprefix(self):
        self.store["user:1"] = "Alice"
        self.store["user:2"] = "Bob"
        self.store["post:1"] = "Hello"
        result = await self.store.aprefix("user:")
        assert len(result) == 2
        assert "user:1" in result

    async def test_aprefix_empty(self):
        result = await self.store.aprefix("missing:")
        assert len(result) == 0

    async def test_ascan(self):
        self.store["cache:active:1"] = "A"
        self.store["cache:active:2"] = "B"
        self.store["cache:inactive:1"] = "C"
        result = await self.store.ascan("cache:active:*")
        assert len(result) == 2

    async def test_ascan_all(self):
        self.store["a"] = 1
        self.store["b"] = 2
        result = await self.store.ascan("*")
        assert len(result) == 2


# ===================================================================
# KVStore async — TTL
# ===================================================================

class TestAsyncKVTTL:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.store = KVStore("async_kv_ttl", key_type=str)
        yield
        self.store.clear()

    async def test_attl(self):
        self.store.set("temp", "val", ttl=3600)
        remaining = await self.store.attl("temp")
        assert remaining is not None
        assert remaining > 3500

    async def test_attl_no_expiry(self):
        self.store["permanent"] = "val"
        ttl = await self.store.attl("permanent")
        assert ttl is None

    async def test_aexpire(self):
        self.store["key"] = "val"
        result = await self.store.aexpire("key", 600)
        assert result is True
        ttl = self.store.ttl("key")
        assert ttl is not None
        assert ttl > 500

    async def test_apersist(self):
        self.store.set("key", "val", ttl=3600)
        result = await self.store.apersist("key")
        assert result is True
        assert self.store.ttl("key") is None

    async def test_apurge_expired(self):
        # Set a key with already-expired TTL by setting ttl very short
        # then purge; since the key may not be expired yet, just verify
        # the method runs without error
        self.store.set("short", "val", ttl=0.001)
        import time
        time.sleep(0.01)
        purged = await self.store.apurge_expired()
        assert isinstance(purged, int)


# ===================================================================
# KVStore async — stats
# ===================================================================

class TestAsyncKVStats:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.store = KVStore("async_kv_stats", key_type=str)
        yield
        self.store.clear()

    async def test_astats(self):
        self.store["a"] = 1
        self.store["b"] = 2
        stats = await self.store.astats()
        assert stats["total_keys"] == 2
        assert stats["active_keys"] == 2
        assert "str" in stats["key_format_counts"]

    async def test_astats_empty(self):
        stats = await self.store.astats()
        assert stats["total_keys"] == 0
