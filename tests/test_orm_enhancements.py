"""Tests for enhanced ORM features beyond the original baseline suite."""

from __future__ import annotations

import datetime

import pytest

from obele import (
    Avg,
    BooleanField,
    Count,
    Database,
    DateTimeField,
    FieldValidationError,
    F,
    ForeignKeyField,
    Func,
    IntegerField,
    Model,
    Q,
    QuerySet,
    RawSQL,
    Subquery,
    TextField,
)


@pytest.fixture(autouse=True)
def setup_db():
    Database.configure(":memory:")
    yield
    Database.close()


class TestScopedDatabase:
    def test_database_using_scopes_work_without_mutating_global(self, tmp_path):
        global_db = tmp_path / "global.sqlite3"
        scoped_db = tmp_path / "scoped.sqlite3"

        Database.configure(str(global_db))
        Database.execute("CREATE TABLE global_items (id INTEGER PRIMARY KEY, val TEXT)")
        Database.execute("INSERT INTO global_items (val) VALUES (?)", ["global"])

        with Database.using(str(scoped_db)):
            path, _ = Database.current_config()
            assert path == str(scoped_db)
            Database.execute("CREATE TABLE scoped_items (id INTEGER PRIMARY KEY, val TEXT)")
            Database.execute("INSERT INTO scoped_items (val) VALUES (?)", ["scoped"])

        path, _ = Database.current_config()
        assert path == str(global_db)
        row = Database.execute_read("SELECT val FROM global_items").fetchone()
        assert row["val"] == "global"


class TestMigrations:
    def test_migrate_adds_columns_and_preserves_data(self):
        class LegacyPerson(Model):
            table_name = "people"
            name = TextField()

        class MigratedPerson(Model):
            table_name = "people"
            name = TextField()
            age = IntegerField(default=30)
            active = BooleanField(default=True, index=True)

        LegacyPerson.create_table()
        LegacyPerson.create(name="Alice")

        MigratedPerson.migrate()

        person = MigratedPerson.get(name="Alice")
        assert person.name == "Alice"
        assert person.age == 30
        assert person.active is True

    def test_migrate_supports_column_renames(self):
        class LegacyCustomer(Model):
            table_name = "customers"
            name = TextField()

        class MigratedCustomer(Model):
            table_name = "customers"
            full_name = TextField(column_name="full_name")

        LegacyCustomer.create_table()
        LegacyCustomer.create(name="Alice")

        MigratedCustomer.migrate(rename_fields={"full_name": "name"})

        customer = MigratedCustomer.get(full_name="Alice")
        assert customer.full_name == "Alice"

    def test_migrate_respects_sqlite_db_defaults(self):
        class LegacyEvent(Model):
            table_name = "events"
            title = TextField()

        class MigratedEvent(Model):
            table_name = "events"
            title = TextField()
            created_at = DateTimeField(nullable=False, db_default="CURRENT_TIMESTAMP")

        LegacyEvent.create_table()
        LegacyEvent.create(title="Launch")

        MigratedEvent.migrate()

        event = MigratedEvent.get(title="Launch")
        assert isinstance(event.created_at, datetime.datetime)


class TestQueryExpressions:
    def test_q_objects_support_or_and_negation(self):
        class QueryUser(Model):
            table_name = "query_users"
            name = TextField()
            age = IntegerField()

        QueryUser.create_table()
        QueryUser.create(name="Alice", age=30)
        QueryUser.create(name="Bob", age=20)
        QueryUser.create(name="Carol", age=40)

        results = QueryUser.filter(Q(name="Alice") | Q(age__lt=25)).order_by("name").all()
        assert [user.name for user in results] == ["Alice", "Bob"]

        negated = QueryUser.filter(~Q(age__lt=30)).order_by("name").all()
        assert [user.name for user in negated] == ["Alice", "Carol"]

    def test_relation_traversal_and_subqueries_work(self):
        class RelationUser(Model):
            table_name = "relation_users"
            name = TextField()
            age = IntegerField()

        class RelationPost(Model):
            table_name = "relation_posts"
            title = TextField()
            author = ForeignKeyField(to=RelationUser)

        RelationUser.create_table()
        RelationPost.create_table()

        alice = RelationUser.create(name="Alice", age=30)
        bob = RelationUser.create(name="Bob", age=20)
        RelationPost.create(title="Hello", author=alice)
        RelationPost.create(title="World", author=bob)

        related = RelationPost.filter(author__name="Alice").all()
        assert [post.title for post in related] == ["Hello"]

        subquery = Subquery(RelationUser.filter(age__gte=30), field="id")
        result = RelationPost.filter(author__in=subquery).all()
        assert [post.title for post in result] == ["Hello"]

    def test_annotations_attach_extra_attributes(self):
        class AnnotatedUser(Model):
            table_name = "annotated_users"
            name = TextField()
            age = IntegerField()

        AnnotatedUser.create_table()
        AnnotatedUser.create(name="Alice", age=30)
        AnnotatedUser.create(name="Beatrice", age=25)

        user = (
            AnnotatedUser.annotate(
                name_length=Func("LENGTH", F("name")),
                age_plus_one=RawSQL("annotated_users.age + 1"),
            )
            .get(name="Alice")
        )

        assert user.name_length == 5
        assert user.age_plus_one == 31
        assert user.to_dict()["name_length"] == 5

        ordered = (
            AnnotatedUser.annotate(name_length=Func("LENGTH", F("name")))
            .order_by("-name_length", "name")
            .all()
        )
        assert [item.name for item in ordered] == ["Beatrice", "Alice"]

    def test_iter_does_not_call_all(self, monkeypatch):
        class StreamingUser(Model):
            table_name = "streaming_users"
            name = TextField()

        StreamingUser.create_table()
        StreamingUser.create(name="A")
        StreamingUser.create(name="B")

        def fail_all(self):
            raise AssertionError("__iter__ should not call all()")

        monkeypatch.setattr(QuerySet, "all", fail_all)
        names = [user.name for user in StreamingUser.order_by("id")]
        assert names == ["A", "B"]


class TestRelationsAndSerialization:
    def test_select_related_hydrates_related_models_and_assignment_accepts_instances(self):
        class HydrationUser(Model):
            table_name = "hydration_users"
            name = TextField()

        class HydrationPost(Model):
            table_name = "hydration_posts"
            title = TextField()
            author = ForeignKeyField(to=HydrationUser)

        HydrationUser.create_table()
        HydrationPost.create_table()

        alice = HydrationUser.create(name="Alice")
        post = HydrationPost.create(title="Hello", author=alice)

        plain = HydrationPost.get(id=post.id)
        assert plain.author == alice.id

        hydrated = HydrationPost.select_related("author").get(id=post.id)
        assert isinstance(hydrated.author, HydrationUser)
        assert hydrated.author.name == "Alice"
        assert hydrated.to_db_dict()["author"] == alice.id

    def test_reverse_relations_expose_queryset_like_access(self):
        class ReverseUser(Model):
            table_name = "reverse_users"
            name = TextField()

        class ReversePost(Model):
            table_name = "reverse_posts"
            title = TextField()
            author = ForeignKeyField(to=ReverseUser)

        ReverseUser.create_table()
        ReversePost.create_table()

        user = ReverseUser.create(name="Alice")
        user.reversepost_set.create(title="One")
        user.reversepost_set.create(title="Two")

        assert user.reversepost_set.count() == 2
        titles = [post.title for post in user.reversepost_set.order_by("title").all()]
        assert titles == ["One", "Two"]

    def test_to_dict_returns_python_values_and_to_db_dict_serializes(self):
        class Event(Model):
            table_name = "events_serialization"
            happened_at = DateTimeField()
            active = BooleanField(default=True)

        Event.create_table()
        now = datetime.datetime(2025, 6, 15, 12, 30, 0)
        event = Event.create(happened_at=now, active=True)

        python_dict = event.to_dict()
        db_dict = event.to_db_dict()

        assert python_dict["happened_at"] == now
        assert python_dict["active"] is True
        assert db_dict["happened_at"] == now.isoformat()
        assert db_dict["active"] == 1


class TestBulkValidation:
    def test_bulk_create_validates_values(self):
        class Tag(Model):
            table_name = "validated_tags"
            label = TextField(max_length=5)

        Tag.create_table()

        with pytest.raises(FieldValidationError, match="max_length"):
            Tag.bulk_create([{"label": "toolong"}])

    def test_bulk_create_validate_false_skips_validation(self):
        class Tag(Model):
            table_name = "bulk_validate_tags"
            label = TextField(max_length=5)

        Tag.create_table()

        created = Tag.bulk_create([{"label": "toolong"}], validate=False)
        assert len(created) == 1

    def test_queryset_update_validates_values(self):
        class BulkUser(Model):
            table_name = "bulk_users"
            name = TextField()
            active = BooleanField(default=True)

        BulkUser.create_table()
        BulkUser.create(name="Alice", active=True)

        with pytest.raises(FieldValidationError):
            BulkUser.filter(name="Alice").update(active="not-a-bool")

    def test_queryset_update_validate_false_skips_validation(self):
        class BulkUser(Model):
            table_name = "bulk_users_skip_validation"
            name = TextField()
            active = BooleanField(default=True)

        BulkUser.create_table()
        BulkUser.create(name="Alice", active=True)

        updated = BulkUser.filter(name="Alice").update(validate=False, active="not-a-bool")
        assert updated == 1

    def test_aggregate_annotations_work(self):
        class AggregateUser(Model):
            table_name = "aggregate_users"
            name = TextField()

        class AggregatePost(Model):
            table_name = "aggregate_posts"
            title = TextField()
            author = ForeignKeyField(to=AggregateUser, related_name="posts")

        AggregateUser.create_table()
        AggregatePost.create_table()

        alice = AggregateUser.create(name="Alice")
        bob = AggregateUser.create(name="Bob")
        AggregatePost.create(title="One", author=alice)
        AggregatePost.create(title="Two", author=alice)
        AggregatePost.create(title="Three", author=bob)

        users = (
            AggregateUser.join("posts")
            .annotate(post_count=Count(F("posts__id")), avg_name_len=Avg(Func("LENGTH", F("name"))))
            .order_by("-post_count", "name")
            .all()
        )

        assert [(user.name, user.post_count) for user in users] == [("Alice", 2), ("Bob", 1)]
        assert all(user.avg_name_len is not None for user in users)

