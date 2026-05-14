"""Tests for new features: fields, constraints, signals, mixins, pagination, FTS."""

import datetime
import enum
import ipaddress
import pytest
from obele import (
    Database, Model, TextField, IntegerField, BooleanField,
    ForeignKeyField, EnumField, TimeField, SlugField, EmailField,
    PickleField, IPAddressField, TimestampMixin, SoftDeleteMixin,
    Page, CursorPage, SearchIndex,
    pre_save, post_save, receiver,
    FieldValidationError, IntegrityError,
)


class Priority(enum.Enum):
    LOW = "low"
    MED = "medium"
    HIGH = "high"


class Task(Model):
    table_name = "tasks_ft"
    title = SlugField(max_length=100)
    priority = EnumField(enum_class=Priority, default=Priority.MED)
    scheduled = TimeField(nullable=True)
    email = EmailField(nullable=True)
    ip = IPAddressField(nullable=True)
    data = PickleField(nullable=True)


class TestNewFields:
    @pytest.fixture(autouse=True)
    def setup(self):
        Task.create_table()

    def test_enum_field(self):
        t = Task.create(title="fix-bug", priority=Priority.HIGH)
        assert t.priority == Priority.HIGH
        t.refresh()
        assert t.priority == Priority.HIGH

    def test_time_field(self):
        t = Task.create(title="morning", scheduled=datetime.time(9, 30))
        t.refresh()
        assert t.scheduled == datetime.time(9, 30)

    def test_slug_valid(self):
        Task.create(title="my-slug-123")

    def test_slug_invalid(self):
        with pytest.raises(FieldValidationError):
            Task.create(title="INVALID SLUG!")

    def test_email_valid(self):
        Task.create(title="user-one", email="test@example.com")

    def test_email_invalid(self):
        with pytest.raises(FieldValidationError):
            Task.create(title="user-two", email="not-an-email")

    def test_ip_v4(self):
        t = Task.create(title="v4", ip="192.168.1.1")
        assert t.ip == ipaddress.IPv4Address("192.168.1.1")
        t.refresh()
        assert isinstance(t.ip, ipaddress.IPv4Address)

    def test_ip_v6(self):
        t = Task.create(title="v6", ip="::1")
        assert t.ip == ipaddress.IPv6Address("::1")

    def test_pickle_field(self):
        obj = {"list": [1, 2, 3], "nested": {"a": True}}
        t = Task.create(title="pickle-test", data=obj)
        t.refresh()
        assert t.data == obj


# ---- Constraints ----------------------------------------------------------

class Constrained(Model):
    table_name = "constrained_ft"
    user_id = IntegerField()
    role = TextField()
    unique_together = [("user_id", "role")]


class TestConstraints:
    @pytest.fixture(autouse=True)
    def setup(self):
        Constrained.create_table()

    def test_unique_allows_different(self):
        Constrained.create(user_id=1, role="admin")
        Constrained.create(user_id=1, role="editor")
        assert Constrained.count() == 2

    def test_unique_rejects_duplicate(self):
        Constrained.create(user_id=1, role="admin")
        with pytest.raises(IntegrityError):
            Constrained.create(user_id=1, role="admin")


# ---- Signals --------------------------------------------------------------

class SigModel(Model):
    table_name = "sig_items_ft"
    name = TextField()


class TestSignals:
    @pytest.fixture(autouse=True)
    def setup(self):
        SigModel.create_table()
        self.log = []

    def test_pre_and_post_save(self):
        def on_pre(sender, instance, **kw):
            self.log.append("pre")
        def on_post(sender, instance, created, **kw):
            self.log.append(f"post:{created}")
        pre_save.connect(on_pre, sender=SigModel)
        post_save.connect(on_post, sender=SigModel)
        try:
            SigModel.create(name="Test")
            assert "pre" in self.log
            assert "post:True" in self.log
        finally:
            pre_save.disconnect(on_pre, sender=SigModel)
            post_save.disconnect(on_post, sender=SigModel)


# ---- Mixins ---------------------------------------------------------------

class TSModel(TimestampMixin, Model):
    table_name = "ts_model_ft"
    name = TextField()

class SoftModel(SoftDeleteMixin, Model):
    table_name = "soft_model_ft"
    name = TextField()


class TestTimestampMixin:
    @pytest.fixture(autouse=True)
    def setup(self):
        TSModel.create_table()

    def test_auto_timestamps(self):
        item = TSModel.create(name="A")
        assert isinstance(item.created_at, datetime.datetime)
        assert isinstance(item.updated_at, datetime.datetime)


class TestSoftDeleteMixin:
    @pytest.fixture(autouse=True)
    def setup(self):
        SoftModel.create_table()

    def test_soft_delete(self):
        item = SoftModel.create(name="A")
        item.delete()
        assert item.is_deleted is True

    def test_default_excludes_deleted(self):
        SoftModel.create(name="A")
        b = SoftModel.create(name="B")
        b.delete()
        assert SoftModel.count() == 1

    def test_with_deleted(self):
        SoftModel.create(name="A")
        b = SoftModel.create(name="B")
        b.delete()
        assert SoftModel.with_deleted().count() == 2

    def test_restore(self):
        item = SoftModel.create(name="A")
        item.delete()
        item.restore()
        assert SoftModel.count() == 1

    def test_hard_delete(self):
        item = SoftModel.create(name="A")
        item.hard_delete()
        assert SoftModel.with_deleted().count() == 0


# ---- Pagination -----------------------------------------------------------

class PagModel(Model):
    table_name = "pag_ft"
    rank = IntegerField()

class TestPagination:
    @pytest.fixture(autouse=True)
    def setup(self):
        PagModel.create_table()
        PagModel.bulk_create([{"rank": i} for i in range(50)])

    def test_offset_pagination(self):
        page = PagModel.order_by("rank").paginate(page=2, per_page=10)
        assert isinstance(page, Page)
        assert len(page.items) == 10
        assert page.total == 50
        assert page.pages == 5

    def test_cursor_pagination(self):
        p = PagModel.order_by("id").cursor_paginate(per_page=10)
        assert isinstance(p, CursorPage)
        assert len(p.items) == 10
        assert p.has_next is True


# ---- FTS ------------------------------------------------------------------

class Doc(Model):
    table_name = "docs_ft"
    title = TextField()
    body = TextField()

class TestFTS:
    @pytest.fixture(autouse=True)
    def setup(self):
        Doc.create_table()
        self.idx = SearchIndex(Doc, fields=["title", "body"])
        self.idx.create()
        yield
        self.idx.drop()

    def test_search(self):
        Doc.create(title="Python Async", body="asyncio is great")
        Doc.create(title="SQLite Tips", body="WAL mode")
        self.idx.rebuild()
        assert len(self.idx.search("python")) >= 1

    def test_empty_search(self):
        assert self.idx.search("") == []


# ---- Database Features ----------------------------------------------------

class TestDatabaseFeatures:
    def test_pool_status(self):
        s = Database.pool_status()
        assert "active_connections" in s

    def test_integrity_check(self):
        assert Database.integrity_check() == "ok"
