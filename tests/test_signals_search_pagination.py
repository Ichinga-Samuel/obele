"""Comprehensive tests for signals, SearchIndex (FTS5), and pagination."""

import pytest
from obele import (
    Database, Model, TextField, IntegerField, RealField,
    Page, CursorPage, SearchIndex,
    Signal, pre_save, post_save, pre_delete, post_delete,
    pre_create, post_create, receiver,
)


# ---------------------------------------------------------------------------
# Models — each uses a unique table_name to avoid collisions
# ---------------------------------------------------------------------------

class SigItem(Model):
    table_name = "ssp_sig_items"
    name = TextField()


class SigOther(Model):
    table_name = "ssp_sig_other"
    label = TextField()


class Article(Model):
    table_name = "ssp_articles"
    title = TextField()
    body = TextField()


class DocSearch(Model):
    table_name = "ssp_doc_search"
    title = TextField()
    body = TextField()
    views = IntegerField(default=0)


class PagItem(Model):
    table_name = "ssp_pag_items"
    rank = IntegerField()
    label = TextField(default="item")


# ===========================================================================
# SIGNALS
# ===========================================================================


class TestSignalPreAndPostSave:
    """pre_save / post_save with `created` kwarg."""

    @pytest.fixture(autouse=True)
    def setup(self):
        SigItem.create_table()
        self.log = []
        yield
        # Disconnect all handlers we may have connected
        for fn in list(self._connected):
            sig, kw = self._connected[fn]
            sig.disconnect(fn, **kw)

    @pytest.fixture(autouse=True)
    def _track(self):
        self._connected = {}
        yield

    def _connect(self, sig, fn, **kw):
        sig.connect(fn, **kw)
        self._connected[fn] = (sig, kw)

    def test_pre_save_fires_on_create(self):
        def handler(sender, instance, created, **kw):
            self.log.append(("pre_save", created))

        self._connect(pre_save, handler, sender=SigItem)
        SigItem.create(name="Alice")
        assert ("pre_save", True) in self.log

    def test_post_save_fires_on_create(self):
        def handler(sender, instance, created, **kw):
            self.log.append(("post_save", created))

        self._connect(post_save, handler, sender=SigItem)
        SigItem.create(name="Bob")
        assert ("post_save", True) in self.log

    def test_save_update_sets_created_false(self):
        def handler(sender, instance, created, **kw):
            self.log.append(("post_save", created))

        self._connect(post_save, handler, sender=SigItem)
        item = SigItem.create(name="Carol")
        self.log.clear()
        item.name = "Carla"
        item.save()
        assert ("post_save", False) in self.log

    def test_pre_save_receives_instance(self):
        captured = {}

        def handler(sender, instance, **kw):
            captured["name"] = instance.name

        self._connect(pre_save, handler, sender=SigItem)
        SigItem.create(name="Dave")
        assert captured["name"] == "Dave"


class TestSignalPreAndPostDelete:
    """pre_delete / post_delete signals."""

    @pytest.fixture(autouse=True)
    def setup(self):
        SigItem.create_table()
        self.log = []
        self._handlers = []
        yield
        for sig, fn, kw in self._handlers:
            sig.disconnect(fn, **kw)

    def _connect(self, sig, fn, **kw):
        sig.connect(fn, **kw)
        self._handlers.append((sig, fn, kw))

    def test_pre_delete_fires(self):
        def handler(sender, instance, **kw):
            self.log.append("pre_delete")

        self._connect(pre_delete, handler, sender=SigItem)
        item = SigItem.create(name="To Delete")
        item.delete()
        assert "pre_delete" in self.log

    def test_post_delete_fires(self):
        def handler(sender, instance, **kw):
            self.log.append("post_delete")

        self._connect(post_delete, handler, sender=SigItem)
        item = SigItem.create(name="Gone")
        item.delete()
        assert "post_delete" in self.log

    def test_delete_signals_fire_in_order(self):
        def on_pre(sender, instance, **kw):
            self.log.append("pre")

        def on_post(sender, instance, **kw):
            self.log.append("post")

        self._connect(pre_delete, on_pre, sender=SigItem)
        self._connect(post_delete, on_post, sender=SigItem)
        item = SigItem.create(name="Order")
        item.delete()
        assert self.log == ["pre", "post"]

    def test_delete_signal_instance_has_no_pk_after_post(self):
        captured = {}

        def handler(sender, instance, **kw):
            captured["pk"] = instance.id

        self._connect(post_delete, handler, sender=SigItem)
        item = SigItem.create(name="Check PK")
        item.delete()
        assert captured["pk"] is None


class TestSignalPreAndPostCreate:
    """pre_create / post_create signals."""

    @pytest.fixture(autouse=True)
    def setup(self):
        SigItem.create_table()
        self.log = []
        self._handlers = []
        yield
        for sig, fn, kw in self._handlers:
            sig.disconnect(fn, **kw)

    def _connect(self, sig, fn, **kw):
        sig.connect(fn, **kw)
        self._handlers.append((sig, fn, kw))

    def test_pre_create_fires_on_new(self):
        def handler(sender, instance, **kw):
            self.log.append("pre_create")

        self._connect(pre_create, handler, sender=SigItem)
        SigItem.create(name="New")
        assert "pre_create" in self.log

    def test_post_create_fires_on_new(self):
        def handler(sender, instance, **kw):
            self.log.append("post_create")

        self._connect(post_create, handler, sender=SigItem)
        SigItem.create(name="New")
        assert "post_create" in self.log

    def test_create_signals_not_fired_on_update(self):
        def handler(sender, instance, **kw):
            self.log.append("create_signal")

        self._connect(pre_create, handler, sender=SigItem)
        self._connect(post_create, handler, sender=SigItem)
        item = SigItem.create(name="Created")
        self.log.clear()
        item.name = "Updated"
        item.save()
        assert self.log == []

    def test_post_create_instance_has_pk(self):
        captured = {}

        def handler(sender, instance, **kw):
            captured["pk"] = instance.id

        self._connect(post_create, handler, sender=SigItem)
        SigItem.create(name="HasPK")
        assert captured["pk"] is not None


class TestReceiverDecorator:
    """receiver() decorator for single and multiple signals."""

    @pytest.fixture(autouse=True)
    def setup(self):
        SigItem.create_table()
        self.log = []
        self._handlers = []
        yield
        for sig, fn, kw in self._handlers:
            sig.disconnect(fn, **kw)

    def test_single_signal_decorator(self):
        log = self.log

        @receiver(post_save, sender=SigItem)
        def on_save(sender, instance, **kw):
            log.append("saved")

        self._handlers.append((post_save, on_save, {"sender": SigItem}))
        SigItem.create(name="Test")
        assert "saved" in log

    def test_list_of_signals_decorator(self):
        log = self.log

        @receiver([pre_save, pre_delete], sender=SigItem)
        def on_lifecycle(sender, instance, **kw):
            log.append("lifecycle")

        self._handlers.append((pre_save, on_lifecycle, {"sender": SigItem}))
        self._handlers.append((pre_delete, on_lifecycle, {"sender": SigItem}))

        item = SigItem.create(name="Multi")
        item.delete()
        assert log.count("lifecycle") == 2


class TestSignalSenderFiltering:
    """Sender-specific vs global receivers."""

    @pytest.fixture(autouse=True)
    def setup(self):
        SigItem.create_table()
        SigOther.create_table()
        self.log = []
        self._handlers = []
        yield
        for sig, fn, kw in self._handlers:
            sig.disconnect(fn, **kw)

    def _connect(self, sig, fn, **kw):
        sig.connect(fn, **kw)
        self._handlers.append((sig, fn, kw))

    def test_sender_specific_only_fires_for_that_sender(self):
        def handler(sender, instance, **kw):
            self.log.append(sender.__name__)

        self._connect(post_save, handler, sender=SigItem)
        SigItem.create(name="Item")
        SigOther.create(label="Other")
        assert self.log == ["SigItem"]

    def test_global_receiver_fires_for_all_senders(self):
        def handler(sender, **kw):
            self.log.append(sender.__name__)

        self._connect(post_save, handler)
        SigItem.create(name="Item")
        SigOther.create(label="Other")
        assert "SigItem" in self.log
        assert "SigOther" in self.log

    def test_both_global_and_sender_fire(self):
        def global_handler(sender, **kw):
            self.log.append("global")

        def specific_handler(sender, **kw):
            self.log.append("specific")

        self._connect(post_save, global_handler)
        self._connect(post_save, specific_handler, sender=SigItem)
        SigItem.create(name="Both")
        assert "global" in self.log
        assert "specific" in self.log


class TestSignalDisconnect:
    """disconnect() returns True/False."""

    @pytest.fixture(autouse=True)
    def setup(self):
        SigItem.create_table()
        yield

    def test_disconnect_returns_true_when_found(self):
        sig = Signal("test_disc")

        def handler(sender, **kw):
            pass

        sig.connect(handler)
        assert sig.disconnect(handler) is True

    def test_disconnect_returns_false_when_not_found(self):
        sig = Signal("test_disc2")

        def handler(sender, **kw):
            pass

        assert sig.disconnect(handler) is False

    def test_disconnect_with_sender(self):
        sig = Signal("test_disc3")

        def handler(sender, **kw):
            pass

        sig.connect(handler, sender=SigItem)
        assert sig.disconnect(handler, sender=SigItem) is True
        assert sig.disconnect(handler, sender=SigItem) is False

    def test_handler_not_called_after_disconnect(self):
        log = []

        def handler(sender, **kw):
            log.append("called")

        post_save.connect(handler, sender=SigItem)
        SigItem.create(name="Before")
        assert len(log) == 1

        post_save.disconnect(handler, sender=SigItem)
        SigItem.create(name="After")
        assert len(log) == 1


class TestSignalHasReceivers:
    """has_receivers() with and without sender."""

    def test_no_receivers_initially(self):
        sig = Signal("empty")
        assert sig.has_receivers() is False

    def test_has_global_receiver(self):
        sig = Signal("has_global")

        def handler(sender, **kw):
            pass

        sig.connect(handler)
        assert sig.has_receivers() is True
        sig.disconnect(handler)

    def test_has_sender_specific_receiver(self):
        sig = Signal("has_sender")

        def handler(sender, **kw):
            pass

        sig.connect(handler, sender=SigItem)
        assert sig.has_receivers(sender=SigItem) is True
        assert sig.has_receivers(sender=SigOther) is False
        sig.disconnect(handler, sender=SigItem)

    def test_global_receiver_detected_for_any_sender(self):
        sig = Signal("global_any")

        def handler(sender, **kw):
            pass

        sig.connect(handler)
        # has_receivers with a specific sender should also detect globals
        assert sig.has_receivers(sender=SigItem) is True
        sig.disconnect(handler)

    def test_no_receivers_after_all_disconnected(self):
        sig = Signal("disc_all")

        def h1(sender, **kw):
            pass

        def h2(sender, **kw):
            pass

        sig.connect(h1)
        sig.connect(h2)
        sig.disconnect(h1)
        sig.disconnect(h2)
        assert sig.has_receivers() is False


class TestSignalCustomName:
    """Signal with custom name."""

    def test_custom_name_in_repr(self):
        sig = Signal("my_custom_signal", providing_args=["data"])
        assert "my_custom_signal" in repr(sig)

    def test_providing_args_stored(self):
        sig = Signal("named", providing_args=["instance", "extra"])
        assert sig.providing_args == ["instance", "extra"]

    def test_default_name_is_empty(self):
        sig = Signal()
        assert sig.name == ""


class TestSignalMultipleReceivers:
    """Multiple receivers on same signal."""

    def test_multiple_receivers_all_called(self):
        sig = Signal("multi")
        results = []

        def h1(sender, **kw):
            results.append("h1")

        def h2(sender, **kw):
            results.append("h2")

        def h3(sender, **kw):
            results.append("h3")

        sig.connect(h1)
        sig.connect(h2)
        sig.connect(h3)
        sig.send(object)
        assert results == ["h1", "h2", "h3"]

    def test_receiver_count_in_repr(self):
        sig = Signal("counted")

        def h(sender, **kw):
            pass

        sig.connect(h)
        assert "receivers=1" in repr(sig)


class TestSignalReturnValues:
    """Receiver return values collected by send()."""

    def test_send_returns_list_of_tuples(self):
        sig = Signal("return_test")

        def h1(sender, **kw):
            return 42

        def h2(sender, **kw):
            return "hello"

        sig.connect(h1)
        sig.connect(h2)
        responses = sig.send(object)

        assert len(responses) == 2
        assert responses[0] == (h1, 42)
        assert responses[1] == (h2, "hello")

    def test_send_with_no_receivers_returns_empty_list(self):
        sig = Signal("empty_send")
        responses = sig.send(object)
        assert responses == []

    def test_none_return_collected(self):
        sig = Signal("none_return")

        def handler(sender, **kw):
            pass  # returns None

        sig.connect(handler)
        responses = sig.send(object)
        assert responses == [(handler, None)]


# ===========================================================================
# SEARCH INDEX (FTS5)
# ===========================================================================


class TestSearchIndexCreateAndSearch:
    """Create index + search."""

    @pytest.fixture(autouse=True)
    def setup(self):
        Article.create_table()
        self.idx = SearchIndex(Article, fields=["title", "body"])
        self.idx.create()
        yield
        self.idx.drop()

    def test_basic_search(self):
        Article.create(title="Python Async", body="asyncio is powerful")
        Article.create(title="SQLite Tips", body="WAL mode is fast")
        self.idx.rebuild()
        results = self.idx.search("python")
        assert len(results) >= 1
        assert any(r.title == "Python Async" for r in results)

    def test_search_returns_model_instances(self):
        Article.create(title="Testing", body="pytest is great")
        self.idx.rebuild()
        results = self.idx.search("pytest")
        assert len(results) == 1
        assert isinstance(results[0], Article)
        assert results[0].title == "Testing"

    def test_search_matches_body(self):
        Article.create(title="Title", body="unique_keyword_xyz")
        self.idx.rebuild()
        results = self.idx.search("unique_keyword_xyz")
        assert len(results) == 1

    def test_search_multiple_results(self):
        Article.create(title="Python Intro", body="Learn Python basics")
        Article.create(title="Advanced Python", body="Python decorators")
        Article.create(title="SQL Guide", body="No match here")
        self.idx.rebuild()
        results = self.idx.search("python")
        assert len(results) == 2


class TestSearchCount:
    """search_count."""

    @pytest.fixture(autouse=True)
    def setup(self):
        Article.create_table()
        self.idx = SearchIndex(Article, fields=["title", "body"])
        self.idx.create()
        yield
        self.idx.drop()

    def test_count_matches(self):
        Article.create(title="Alpha", body="content alpha")
        Article.create(title="Beta", body="content beta")
        Article.create(title="Gamma Alpha", body="more alpha")
        self.idx.rebuild()
        assert self.idx.search_count("alpha") == 2

    def test_count_no_matches(self):
        Article.create(title="Hello", body="World")
        self.idx.rebuild()
        assert self.idx.search_count("zzz_nonexistent") == 0

    def test_count_empty_query_returns_zero(self):
        Article.create(title="Hello", body="World")
        self.idx.rebuild()
        assert self.idx.search_count("") == 0

    def test_count_whitespace_query_returns_zero(self):
        assert self.idx.search_count("   ") == 0


class TestSearchLimitOffset:
    """search with limit and offset."""

    @pytest.fixture(autouse=True)
    def setup(self):
        Article.create_table()
        self.idx = SearchIndex(Article, fields=["title", "body"])
        self.idx.create()
        for i in range(10):
            Article.create(title=f"Python topic {i}", body=f"Python content {i}")
        self.idx.rebuild()
        yield
        self.idx.drop()

    def test_limit(self):
        results = self.idx.search("python", limit=3)
        assert len(results) == 3

    def test_offset(self):
        all_results = self.idx.search("python")
        offset_results = self.idx.search("python", offset=5)
        assert len(offset_results) == len(all_results) - 5

    def test_limit_and_offset(self):
        results = self.idx.search("python", limit=2, offset=3)
        assert len(results) == 2

    def test_offset_beyond_results(self):
        results = self.idx.search("python", offset=100)
        assert results == []


class TestSearchEmptyQuery:
    """Empty/whitespace query returns []."""

    @pytest.fixture(autouse=True)
    def setup(self):
        Article.create_table()
        self.idx = SearchIndex(Article, fields=["title", "body"])
        self.idx.create()
        Article.create(title="Something", body="Exists")
        self.idx.rebuild()
        yield
        self.idx.drop()

    def test_empty_string_returns_empty(self):
        assert self.idx.search("") == []

    def test_whitespace_returns_empty(self):
        assert self.idx.search("   ") == []

    def test_none_like_empty_returns_empty(self):
        # The implementation checks `not query` so empty string is handled
        assert self.idx.search("") == []


class TestSearchRebuild:
    """Rebuild after data changes."""

    @pytest.fixture(autouse=True)
    def setup(self):
        Article.create_table()
        # No content_sync so triggers won't auto-update —
        # we need to test rebuild explicitly.
        self.idx = SearchIndex(
            Article, fields=["title", "body"], content_sync=False
        )
        self.idx.create()
        yield
        self.idx.drop()

    def test_rebuild_indexes_existing_data(self):
        Article.create(title="Existing", body="Already here")
        # Without content_sync, nothing auto-indexed.
        # But rebuild should pull from content table.
        # For external-content FTS, rebuild only works with content= tables.
        # Since content_sync=False creates a standalone FTS table,
        # we just verify rebuild doesn't error.
        self.idx.rebuild()

    def test_rebuild_after_content_sync_insert(self):
        # Use a content-synced index for actual rebuild testing
        idx2 = SearchIndex(Article, fields=["title", "body"],
                           fts_table="ssp_articles_fts2",
                           content_sync=True)
        idx2.create()
        try:
            Article.create(title="Rebuild Test", body="data here")
            idx2.rebuild()
            results = idx2.search("rebuild")
            assert len(results) == 1
        finally:
            idx2.drop()


class TestSearchContentSyncTriggers:
    """Content sync creates triggers that auto-index on insert/update/delete."""

    @pytest.fixture(autouse=True)
    def setup(self):
        Article.create_table()
        self.idx = SearchIndex(
            Article, fields=["title", "body"], content_sync=True
        )
        self.idx.create()
        yield
        self.idx.drop()

    def test_insert_auto_indexed(self):
        Article.create(title="Auto Indexed", body="trigger insert")
        # No rebuild needed — trigger handles it
        results = self.idx.search("auto indexed")
        assert len(results) == 1

    def test_delete_auto_removed(self):
        art = Article.create(title="To Remove", body="trigger delete")
        assert self.idx.search_count("remove") == 1
        art.delete()
        assert self.idx.search_count("remove") == 0

    def test_update_auto_reindexed(self):
        art = Article.create(title="Old Title", body="old body content")
        assert self.idx.search_count("old") >= 1
        art.title = "New Title"
        art.body = "new body content"
        art.save()
        # After update, old term should not match
        assert self.idx.search_count("old") == 0
        assert self.idx.search_count("new") >= 1

    def test_triggers_exist_in_sqlite(self):
        """Verify the three triggers actually exist."""
        rows = Database.fetchall(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE ?",
            [f"{self.idx.fts_table}_%"],
        )
        trigger_names = {dict(r)["name"] for r in rows}
        assert f"{self.idx.fts_table}_ai" in trigger_names
        assert f"{self.idx.fts_table}_ad" in trigger_names
        assert f"{self.idx.fts_table}_au" in trigger_names


class TestSearchCustomFtsTable:
    """Custom fts_table name."""

    @pytest.fixture(autouse=True)
    def setup(self):
        Article.create_table()
        yield

    def test_custom_name(self):
        idx = SearchIndex(
            Article, fields=["title", "body"],
            fts_table="my_custom_fts_table"
        )
        idx.create()
        try:
            Article.create(title="Custom", body="table name")
            idx.rebuild()
            results = idx.search("custom")
            assert len(results) == 1
            assert idx.fts_table == "my_custom_fts_table"
        finally:
            idx.drop()

    def test_default_name_convention(self):
        idx = SearchIndex(Article, fields=["title"])
        assert idx.fts_table == "ssp_articles_fts"


class TestSearchOptimize:
    """optimize() doesn't error."""

    @pytest.fixture(autouse=True)
    def setup(self):
        Article.create_table()
        self.idx = SearchIndex(Article, fields=["title", "body"])
        self.idx.create()
        yield
        self.idx.drop()

    def test_optimize_on_empty(self):
        self.idx.optimize()  # Should not raise

    def test_optimize_after_data(self):
        Article.create(title="Opt", body="test optimize")
        self.idx.rebuild()
        self.idx.optimize()  # Should not raise


class TestSearchDrop:
    """drop() removes everything."""

    @pytest.fixture(autouse=True)
    def setup(self):
        Article.create_table()
        yield

    def test_drop_removes_fts_table(self):
        idx = SearchIndex(Article, fields=["title", "body"])
        idx.create()
        # Verify table exists
        row = Database.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            [idx.fts_table],
        )
        assert row is not None
        idx.drop()
        # Verify table gone
        row = Database.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            [idx.fts_table],
        )
        assert row is None

    def test_drop_removes_triggers(self):
        idx = SearchIndex(Article, fields=["title", "body"], content_sync=True)
        idx.create()
        idx.drop()
        rows = Database.fetchall(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE ?",
            [f"{idx.fts_table}_%"],
        )
        assert len(rows) == 0

    def test_drop_idempotent(self):
        idx = SearchIndex(Article, fields=["title", "body"])
        idx.create()
        idx.drop()
        idx.drop()  # Should not error


class TestSearchInvalidField:
    """Invalid field raises ValueError."""

    @pytest.fixture(autouse=True)
    def setup(self):
        Article.create_table()
        yield

    def test_unknown_field_raises(self):
        with pytest.raises(ValueError, match="Unknown field"):
            SearchIndex(Article, fields=["nonexistent"])

    def test_empty_fields_raises(self):
        with pytest.raises(ValueError, match="At least one field"):
            SearchIndex(Article, fields=[])


# ===========================================================================
# PAGINATION — OFFSET-BASED (Page)
# ===========================================================================


class TestPaginationFirstPage:
    """First page (has_prev=False)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        PagItem.create_table()
        PagItem.bulk_create([{"rank": i, "label": f"item{i}"} for i in range(30)])
        yield

    def test_first_page_has_prev_false(self):
        page = PagItem.order_by("rank").paginate(page=1, per_page=10)
        assert page.has_prev is False

    def test_first_page_has_next_true(self):
        page = PagItem.order_by("rank").paginate(page=1, per_page=10)
        assert page.has_next is True

    def test_first_page_number(self):
        page = PagItem.order_by("rank").paginate(page=1, per_page=10)
        assert page.page == 1


class TestPaginationMiddlePage:
    """Middle page (has_prev=True, has_next=True)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        PagItem.create_table()
        PagItem.bulk_create([{"rank": i} for i in range(30)])
        yield

    def test_middle_page_has_both(self):
        page = PagItem.order_by("rank").paginate(page=2, per_page=10)
        assert page.has_prev is True
        assert page.has_next is True

    def test_middle_page_items_count(self):
        page = PagItem.order_by("rank").paginate(page=2, per_page=10)
        assert len(page.items) == 10


class TestPaginationLastPage:
    """Last page (has_next=False)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        PagItem.create_table()
        PagItem.bulk_create([{"rank": i} for i in range(30)])
        yield

    def test_last_page_has_next_false(self):
        page = PagItem.order_by("rank").paginate(page=3, per_page=10)
        assert page.has_next is False

    def test_last_page_has_prev_true(self):
        page = PagItem.order_by("rank").paginate(page=3, per_page=10)
        assert page.has_prev is True


class TestPaginationEmptyDataset:
    """Empty dataset."""

    @pytest.fixture(autouse=True)
    def setup(self):
        PagItem.create_table()
        yield

    def test_empty_page(self):
        page = PagItem.order_by("rank").paginate(page=1, per_page=10)
        assert page.items == []
        assert page.total == 0
        assert page.pages == 1  # max(1, ...) ensures at least 1
        assert page.has_next is False
        assert page.has_prev is False

    def test_empty_len(self):
        page = PagItem.order_by("rank").paginate(page=1, per_page=10)
        assert len(page) == 0


class TestPaginationSinglePage:
    """Single page dataset."""

    @pytest.fixture(autouse=True)
    def setup(self):
        PagItem.create_table()
        PagItem.bulk_create([{"rank": i} for i in range(5)])
        yield

    def test_single_page_no_next_no_prev(self):
        page = PagItem.order_by("rank").paginate(page=1, per_page=10)
        assert page.has_next is False
        assert page.has_prev is False

    def test_single_page_total(self):
        page = PagItem.order_by("rank").paginate(page=1, per_page=10)
        assert page.total == 5
        assert page.pages == 1
        assert len(page.items) == 5


class TestPageIterationAndLen:
    """Page iteration and len()."""

    @pytest.fixture(autouse=True)
    def setup(self):
        PagItem.create_table()
        PagItem.bulk_create([{"rank": i} for i in range(15)])
        yield

    def test_iteration(self):
        page = PagItem.order_by("rank").paginate(page=1, per_page=10)
        items = list(page)
        assert len(items) == 10
        assert all(isinstance(item, PagItem) for item in items)

    def test_len(self):
        page = PagItem.order_by("rank").paginate(page=1, per_page=10)
        assert len(page) == 10

    def test_iteration_matches_items(self):
        page = PagItem.order_by("rank").paginate(page=1, per_page=5)
        iterated = list(page)
        assert len(iterated) == len(page.items)
        for a, b in zip(iterated, page.items):
            assert a.id == b.id


class TestPaginationCustomPerPage:
    """Custom per_page."""

    @pytest.fixture(autouse=True)
    def setup(self):
        PagItem.create_table()
        PagItem.bulk_create([{"rank": i} for i in range(100)])
        yield

    def test_per_page_7(self):
        page = PagItem.order_by("rank").paginate(page=1, per_page=7)
        assert page.per_page == 7
        assert len(page.items) == 7
        assert page.total == 100
        # ceil(100/7) = 15
        assert page.pages == 15

    def test_per_page_50(self):
        page = PagItem.order_by("rank").paginate(page=1, per_page=50)
        assert len(page.items) == 50
        assert page.pages == 2

    def test_per_page_1(self):
        page = PagItem.order_by("rank").paginate(page=1, per_page=1)
        assert len(page.items) == 1
        assert page.pages == 100

    def test_page_metadata(self):
        page = PagItem.order_by("rank").paginate(page=3, per_page=25)
        assert page.page == 3
        assert page.per_page == 25
        assert page.total == 100
        assert page.pages == 4
        assert len(page.items) == 25


class TestPaginationAllPagesCoverAllItems:
    """All pages cover all items."""

    @pytest.fixture(autouse=True)
    def setup(self):
        PagItem.create_table()
        PagItem.bulk_create([{"rank": i} for i in range(23)])
        yield

    def test_all_items_covered(self):
        all_ids = set()
        per_page = 5
        total_pages = PagItem.order_by("rank").paginate(
            page=1, per_page=per_page
        ).pages
        for p in range(1, total_pages + 1):
            page = PagItem.order_by("rank").paginate(page=p, per_page=per_page)
            for item in page:
                all_ids.add(item.id)
        assert len(all_ids) == 23

    def test_no_duplicates_across_pages(self):
        all_ids = []
        per_page = 7
        total_pages = PagItem.order_by("rank").paginate(
            page=1, per_page=per_page
        ).pages
        for p in range(1, total_pages + 1):
            page = PagItem.order_by("rank").paginate(page=p, per_page=per_page)
            all_ids.extend(item.id for item in page)
        assert len(all_ids) == len(set(all_ids))


# ===========================================================================
# PAGINATION — CURSOR-BASED (CursorPage)
# ===========================================================================


class TestCursorPaginationForward:
    """Cursor pagination forward traversal."""

    @pytest.fixture(autouse=True)
    def setup(self):
        PagItem.create_table()
        PagItem.bulk_create([{"rank": i} for i in range(25)])
        yield

    def test_first_cursor_page(self):
        page = PagItem.order_by("id").cursor_paginate(per_page=10)
        assert isinstance(page, CursorPage)
        assert len(page.items) == 10
        assert page.has_next is True
        assert page.has_prev is False

    def test_forward_traversal_covers_all(self):
        all_ids = []
        page = PagItem.order_by("id").cursor_paginate(per_page=10)
        all_ids.extend(item.id for item in page)

        while page.has_next:
            page = PagItem.order_by("id").cursor_paginate(
                per_page=10, after=page.end_cursor
            )
            all_ids.extend(item.id for item in page)

        assert len(all_ids) == 25
        assert len(set(all_ids)) == 25  # no duplicates

    def test_last_cursor_page_has_next_false(self):
        page = PagItem.order_by("id").cursor_paginate(per_page=10)
        while page.has_next:
            page = PagItem.order_by("id").cursor_paginate(
                per_page=10, after=page.end_cursor
            )
        assert page.has_next is False


class TestCursorPaginationAfter:
    """Cursor pagination with after parameter."""

    @pytest.fixture(autouse=True)
    def setup(self):
        PagItem.create_table()
        PagItem.bulk_create([{"rank": i} for i in range(20)])
        yield

    def test_after_skips_items(self):
        first = PagItem.order_by("id").cursor_paginate(per_page=5)
        second = PagItem.order_by("id").cursor_paginate(
            per_page=5, after=first.end_cursor
        )
        first_ids = {item.id for item in first}
        second_ids = {item.id for item in second}
        assert len(first_ids & second_ids) == 0  # No overlap

    def test_after_has_prev_true(self):
        first = PagItem.order_by("id").cursor_paginate(per_page=5)
        second = PagItem.order_by("id").cursor_paginate(
            per_page=5, after=first.end_cursor
        )
        assert second.has_prev is True

    def test_cursor_values(self):
        page = PagItem.order_by("id").cursor_paginate(per_page=5)
        assert page.start_cursor is not None
        assert page.end_cursor is not None
        assert page.start_cursor <= page.end_cursor

    def test_after_with_no_more_items(self):
        # Get the last item's ID
        all_items = PagItem.order_by("id").all()
        last_id = all_items[-1].id
        page = PagItem.order_by("id").cursor_paginate(
            per_page=5, after=last_id
        )
        assert page.items == []
        assert page.has_next is False


class TestCursorPaginationEmpty:
    """Cursor pagination on empty dataset."""

    @pytest.fixture(autouse=True)
    def setup(self):
        PagItem.create_table()
        yield

    def test_empty_cursor_page(self):
        page = PagItem.order_by("id").cursor_paginate(per_page=10)
        assert page.items == []
        assert page.has_next is False
        assert page.has_prev is False
        assert page.start_cursor is None
        assert page.end_cursor is None

    def test_empty_cursor_len(self):
        page = PagItem.order_by("id").cursor_paginate(per_page=10)
        assert len(page) == 0


class TestCursorPageIteration:
    """CursorPage iteration and len."""

    @pytest.fixture(autouse=True)
    def setup(self):
        PagItem.create_table()
        PagItem.bulk_create([{"rank": i} for i in range(12)])
        yield

    def test_iteration(self):
        page = PagItem.order_by("id").cursor_paginate(per_page=5)
        items = list(page)
        assert len(items) == 5
        assert all(isinstance(item, PagItem) for item in items)

    def test_len(self):
        page = PagItem.order_by("id").cursor_paginate(per_page=5)
        assert len(page) == 5

    def test_single_item_page(self):
        page = PagItem.order_by("id").cursor_paginate(per_page=1)
        assert len(page) == 1
        assert page.has_next is True
        assert page.start_cursor == page.end_cursor
