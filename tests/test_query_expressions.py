"""Tests for query system: lookups, Q objects, expressions, set operations."""

import pytest
from obele import (
	Database,
	Model,
	TextField,
	IntegerField,
	RealField,
	BooleanField,
	ForeignKeyField,
	JSONField,
	DateTimeField,
	Q,
	F,
	Value,
	Func,
	RawSQL,
	Subquery,
	Count,
	Sum,
	Avg,
	Min,
	Max,
	QuerySet,
	RecordNotFoundError,
	MultipleResultsError,
	FieldValidationError,
)


class QAuthor(Model):
	table_name = "qe_authors"
	name = TextField()
	age = IntegerField(nullable=True)
	active = BooleanField(default=True)


class QBook(Model):
	table_name = "qe_books"
	title = TextField()
	pages = IntegerField(default=0)
	price = RealField(default=0.0)
	author = ForeignKeyField(to=QAuthor, related_name="qbooks")
	published = BooleanField(default=False)


class QTag(Model):
	table_name = "qe_tags"
	name = TextField(unique=True)


@pytest.fixture(autouse=True)
def create_tables():
	QAuthor.create_table()
	QBook.create_table()
	QTag.create_table()
	yield


class TestLookups:
	def test_exact(self):
		QAuthor.create(name="Alice", age=30)
		QAuthor.create(name="Bob", age=25)
		result = QAuthor.filter(name="Alice").all()
		assert len(result) == 1 and result[0].name == "Alice"

	def test_ne(self):
		QAuthor.create(name="Alice")
		QAuthor.create(name="Bob")
		result = QAuthor.filter(name__ne="Alice").all()
		assert len(result) == 1 and result[0].name == "Bob"

	def test_gt(self):
		QAuthor.create(name="A", age=20)
		QAuthor.create(name="B", age=30)
		result = QAuthor.filter(age__gt=25).all()
		assert len(result) == 1

	def test_gte(self):
		QAuthor.create(name="A", age=25)
		QAuthor.create(name="B", age=30)
		result = QAuthor.filter(age__gte=25).all()
		assert len(result) == 2

	def test_lt(self):
		QAuthor.create(name="A", age=20)
		QAuthor.create(name="B", age=30)
		result = QAuthor.filter(age__lt=25).all()
		assert len(result) == 1

	def test_lte(self):
		QAuthor.create(name="A", age=20)
		QAuthor.create(name="B", age=25)
		result = QAuthor.filter(age__lte=25).all()
		assert len(result) == 2

	def test_in(self):
		QAuthor.create(name="Alice")
		QAuthor.create(name="Bob")
		QAuthor.create(name="Carol")
		result = QAuthor.filter(name__in=["Alice", "Carol"]).all()
		assert len(result) == 2

	def test_in_empty_list(self):
		QAuthor.create(name="Alice")
		result = QAuthor.filter(name__in=[]).all()
		assert len(result) == 0

	def test_not_in(self):
		QAuthor.create(name="Alice")
		QAuthor.create(name="Bob")
		QAuthor.create(name="Carol")
		result = QAuthor.filter(name__not_in=["Alice"]).all()
		assert len(result) == 2

	def test_not_in_empty(self):
		QAuthor.create(name="Alice")
		result = QAuthor.filter(name__not_in=[]).all()
		assert len(result) == 1

	def test_contains(self):
		QAuthor.create(name="Alice")
		QAuthor.create(name="Bob")
		result = QAuthor.filter(name__contains="lic").all()
		assert len(result) == 1

	def test_startswith(self):
		QAuthor.create(name="Alice")
		QAuthor.create(name="Bob")
		result = QAuthor.filter(name__startswith="Ali").all()
		assert len(result) == 1

	def test_endswith(self):
		QAuthor.create(name="Alice")
		QAuthor.create(name="Grace")
		result = QAuthor.filter(name__endswith="ce").all()
		assert len(result) == 2

	def test_iexact(self):
		QAuthor.create(name="Alice")
		result = QAuthor.filter(name__iexact="alice").all()
		assert len(result) == 1

	def test_icontains(self):
		QAuthor.create(name="Alice")
		result = QAuthor.filter(name__icontains="LIC").all()
		assert len(result) == 1

	def test_istartswith(self):
		QAuthor.create(name="Alice")
		result = QAuthor.filter(name__istartswith="ALI").all()
		assert len(result) == 1

	def test_iendswith(self):
		QAuthor.create(name="Alice")
		result = QAuthor.filter(name__iendswith="CE").all()
		assert len(result) == 1

	def test_is_null_true(self):
		QAuthor.create(name="A", age=None)
		QAuthor.create(name="B", age=30)
		result = QAuthor.filter(age__is_null=True).all()
		assert len(result) == 1 and result[0].name == "A"

	def test_is_null_false(self):
		QAuthor.create(name="A", age=None)
		QAuthor.create(name="B", age=30)
		result = QAuthor.filter(age__is_null=False).all()
		assert len(result) == 1 and result[0].name == "B"

	def test_between(self):
		QAuthor.create(name="A", age=10)
		QAuthor.create(name="B", age=20)
		QAuthor.create(name="C", age=30)
		result = QAuthor.filter(age__between=(15, 25)).all()
		assert len(result) == 1

	def test_range(self):
		QAuthor.create(name="A", age=10)
		QAuthor.create(name="B", age=20)
		QAuthor.create(name="C", age=30)
		result = QAuthor.filter(age__range=(10, 20)).all()
		assert len(result) == 2

	def test_exact_none_becomes_is_null(self):
		QAuthor.create(name="A", age=None)
		QAuthor.create(name="B", age=30)
		result = QAuthor.filter(age=None).all()
		assert len(result) == 1 and result[0].name == "A"

	def test_ne_none_becomes_is_not_null(self):
		QAuthor.create(name="A", age=None)
		QAuthor.create(name="B", age=30)
		result = QAuthor.filter(age__ne=None).all()
		assert len(result) == 1 and result[0].name == "B"


class TestQObjects:
	def test_q_and(self):
		QAuthor.create(name="Alice", age=30)
		QAuthor.create(name="Bob", age=30)
		QAuthor.create(name="Carol", age=25)
		result = QAuthor.filter(Q(name="Alice") & Q(age=30)).all()
		assert len(result) == 1

	def test_q_or(self):
		QAuthor.create(name="Alice", age=30)
		QAuthor.create(name="Bob", age=25)
		QAuthor.create(name="Carol", age=35)
		result = QAuthor.filter(Q(name="Alice") | Q(name="Carol")).all()
		assert len(result) == 2

	def test_q_not(self):
		QAuthor.create(name="Alice")
		QAuthor.create(name="Bob")
		result = QAuthor.filter(~Q(name="Alice")).all()
		assert len(result) == 1 and result[0].name == "Bob"

	def test_q_nested(self):
		QAuthor.create(name="Alice", age=30)
		QAuthor.create(name="Bob", age=25)
		QAuthor.create(name="Carol", age=35)
		result = QAuthor.filter((Q(name="Alice") | Q(name="Carol")) & Q(age__gte=30)).all()
		assert len(result) == 2

	def test_q_double_not(self):
		QAuthor.create(name="Alice")
		QAuthor.create(name="Bob")
		result = QAuthor.filter(~~Q(name="Alice")).all()
		assert len(result) == 1 and result[0].name == "Alice"

	def test_q_complex_or_and(self):
		QAuthor.create(name="A", age=10, active=True)
		QAuthor.create(name="B", age=20, active=False)
		QAuthor.create(name="C", age=30, active=True)
		result = QAuthor.filter(Q(age__gte=20) | Q(active=True)).all()
		assert len(result) == 3

	def test_q_with_kwargs(self):
		QAuthor.create(name="Alice", age=30)
		QAuthor.create(name="Bob", age=25)
		result = QAuthor.filter(Q(age__gte=25), name="Alice").all()
		assert len(result) == 1

	def test_multiple_q_args_are_anded(self):
		QAuthor.create(name="Alice", age=30)
		QAuthor.create(name="Bob", age=30)
		result = QAuthor.filter(Q(name="Alice"), Q(age=30)).all()
		assert len(result) == 1


class TestExpressions:
	def test_f_expression_in_filter(self):
		a = QAuthor.create(name="Alice", age=30)
		QBook.create(title="Book1", pages=30, author=a)
		QBook.create(title="Book2", pages=100, author=a)
		# Filter books where pages equals author's age
		result = QBook.filter(pages=F("author__age")).all()
		assert len(result) == 1 and result[0].title == "Book1"

	def test_value_expression(self):
		v = Value(42)
		qs = QuerySet(QAuthor)
		sql, params = v.as_sql(qs)
		assert sql == "?"
		assert params == [42]

	def test_count_expression(self):
		QAuthor.create(name="A", age=10)
		QAuthor.create(name="B", age=20)
		count = QAuthor.aggregate("COUNT", "id")
		assert count == 2

	def test_sum_expression(self):
		QAuthor.create(name="A", age=10)
		QAuthor.create(name="B", age=20)
		total = QAuthor.aggregate("SUM", "age")
		assert total == 30

	def test_avg_expression(self):
		QAuthor.create(name="A", age=10)
		QAuthor.create(name="B", age=20)
		avg = QAuthor.aggregate("AVG", "age")
		assert avg == 15.0

	def test_min_expression(self):
		QAuthor.create(name="A", age=10)
		QAuthor.create(name="B", age=20)
		assert QAuthor.aggregate("MIN", "age") == 10

	def test_max_expression(self):
		QAuthor.create(name="A", age=10)
		QAuthor.create(name="B", age=20)
		assert QAuthor.aggregate("MAX", "age") == 20

	def test_raw_sql_expression(self):
		raw = RawSQL("1 + 1")
		qs = QuerySet(QAuthor)
		sql, params = raw.as_sql(qs)
		assert sql == "1 + 1"
		assert params == []

	def test_raw_sql_with_params(self):
		raw = RawSQL("? + ?", [1, 2])
		qs = QuerySet(QAuthor)
		sql, params = raw.as_sql(qs)
		assert sql == "? + ?"
		assert params == [1, 2]

	def test_subquery_in_filter(self):
		a1 = QAuthor.create(name="Alice", age=30)
		a2 = QAuthor.create(name="Bob", age=25)
		QBook.create(title="B1", pages=100, author=a1)
		# Find authors who have books
		book_author_ids = QBook.values_list("author", flat=True)
		result = QAuthor.filter(id__in=Subquery(book_author_ids)).all()
		assert len(result) == 1 and result[0].name == "Alice"


class TestAnnotateGroupBy:
	def test_annotate_with_count(self):
		a = QAuthor.create(name="Alice")
		QBook.create(title="B1", author=a)
		QBook.create(title="B2", author=a)
		results = QAuthor.join("qbooks").annotate(book_count=Count(F("qbooks__id"))).group_by("id").all()
		assert results[0].book_count == 2

	def test_annotate_with_sum(self):
		a = QAuthor.create(name="Alice")
		QBook.create(title="B1", pages=100, author=a)
		QBook.create(title="B2", pages=200, author=a)
		results = QAuthor.join("qbooks").annotate(total_pages=Sum(F("qbooks__pages"))).group_by("id").all()
		assert results[0].total_pages == 300

	def test_group_by(self):
		a1 = QAuthor.create(name="Alice")
		a2 = QAuthor.create(name="Bob")
		QBook.create(title="B1", author=a1)
		QBook.create(title="B2", author=a1)
		QBook.create(title="B3", author=a2)
		results = QBook.annotate(count=Count("id")).group_by("author").all()
		assert len(results) == 2

	def test_having(self):
		a1 = QAuthor.create(name="Alice")
		a2 = QAuthor.create(name="Bob")
		QBook.create(title="B1", author=a1)
		QBook.create(title="B2", author=a1)
		QBook.create(title="B3", author=a2)
		results = QBook.annotate(cnt=Count("id")).group_by("author").having(cnt__gte=2).all()
		assert len(results) == 1


class TestQuerySetMethods:
	def test_distinct(self):
		QAuthor.create(name="Alice", age=30)
		QAuthor.create(name="Bob", age=30)
		result = QAuthor.values_list("age", flat=True).distinct().all()
		assert len(result) == 1 and result[0] == 30

	def test_values(self):
		QAuthor.create(name="Alice", age=30)
		result = QAuthor.values("name", "age").all()
		assert result == [{"name": "Alice", "age": 30}]

	def test_values_all_fields(self):
		QAuthor.create(name="Alice", age=30)
		result = QAuthor.values().first()
		assert "name" in result and "age" in result and "id" in result

	def test_values_list(self):
		QAuthor.create(name="Alice", age=30)
		result = QAuthor.values_list("name", "age").all()
		assert result[0] == ("Alice", 30)

	def test_values_list_flat(self):
		QAuthor.create(name="Alice")
		QAuthor.create(name="Bob")
		names = QAuthor.order_by("name").values_list("name", flat=True).all()
		assert names == ["Alice", "Bob"]

	def test_only(self):
		QAuthor.create(name="Alice", age=30)
		result = QAuthor.only("name").first()
		assert result.name == "Alice"
		assert result.id is not None  # PK always included

	def test_defer(self):
		QAuthor.create(name="Alice", age=30)
		result = QAuthor.defer("age").first()
		assert result.name == "Alice"
		assert result.id is not None

	def test_order_by_asc(self):
		QAuthor.create(name="C")
		QAuthor.create(name="A")
		QAuthor.create(name="B")
		names = [a.name for a in QAuthor.order_by("name").all()]
		assert names == ["A", "B", "C"]

	def test_order_by_desc(self):
		QAuthor.create(name="A", age=1)
		QAuthor.create(name="B", age=2)
		ages = [a.age for a in QAuthor.order_by("-age").all()]
		assert ages == [2, 1]

	def test_limit(self):
		for i in range(10):
			QAuthor.create(name=f"u{i}", age=i)
		result = QAuthor.limit(3).all()
		assert len(result) == 3

	def test_offset(self):
		for i in range(10):
			QAuthor.create(name=f"u{i}", age=i)
		result = QAuthor.order_by("age").offset(5).limit(3).all()
		assert result[0].age == 5

	def test_count(self):
		QAuthor.create(name="A")
		QAuthor.create(name="B")
		assert QAuthor.count() == 2

	def test_exists_true(self):
		QAuthor.create(name="A")
		assert QAuthor.exists()

	def test_exists_false(self):
		assert not QAuthor.exists()

	def test_first_returns_instance(self):
		QAuthor.create(name="Alice")
		result = QAuthor.first()
		assert result.name == "Alice"

	def test_first_returns_none(self):
		assert QAuthor.first() is None

	def test_get_success(self):
		QAuthor.create(name="Alice")
		result = QAuthor.get(name="Alice")
		assert result.name == "Alice"

	def test_get_not_found(self):
		with pytest.raises(RecordNotFoundError):
			QAuthor.get(name="Nobody")

	def test_get_multiple(self):
		QAuthor.create(name="Alice")
		QAuthor.create(name="Alice")
		with pytest.raises(MultipleResultsError):
			QAuthor.get(name="Alice")

	def test_exclude(self):
		QAuthor.create(name="Alice")
		QAuthor.create(name="Bob")
		result = QAuthor.exclude(name="Alice").all()
		assert len(result) == 1 and result[0].name == "Bob"

	def test_as_sql(self):
		qs = QAuthor.filter(name="Alice")
		sql, params = qs.as_sql()
		assert "SELECT" in sql
		assert "WHERE" in sql
		assert "Alice" in params

	def test_explain(self):
		QAuthor.create(name="Alice")
		plan = QAuthor.filter(name="Alice").explain()
		assert isinstance(plan, str)
		assert len(plan) > 0

	def test_iterator(self):
		for i in range(5):
			QAuthor.create(name=f"u{i}", age=i)
		results = list(QAuthor.order_by("age").iterator(chunk_size=2))
		assert len(results) == 5
		assert results[0].age == 0

	def test_bulk_update(self):
		QAuthor.create(name="A", age=10)
		QAuthor.create(name="B", age=20)
		updated = QAuthor.filter(age__lt=15).update(age=99)
		assert updated == 1
		assert QAuthor.get(name="A").age == 99

	def test_bulk_delete(self):
		QAuthor.create(name="A")
		QAuthor.create(name="B")
		deleted = QAuthor.filter(name="A").delete()
		assert deleted == 1
		assert QAuthor.count() == 1


class TestJoinsAndRelations:
	def test_filter_across_fk(self):
		a1 = QAuthor.create(name="Alice")
		a2 = QAuthor.create(name="Bob")
		QBook.create(title="B1", author=a1)
		QBook.create(title="B2", author=a2)
		result = QBook.filter(author__name="Alice").all()
		assert len(result) == 1 and result[0].title == "B1"

	def test_select_related(self):
		a = QAuthor.create(name="Alice")
		QBook.create(title="Novel", author=a)
		book = QBook.select_related("author").first()
		assert book.author.name == "Alice"

	def test_prefetch_related(self):
		a = QAuthor.create(name="Alice")
		QBook.create(title="B1", author=a)
		QBook.create(title="B2", author=a)
		authors = QAuthor.prefetch_related("qbooks").all()
		assert len(authors) == 1
		books = authors[0]._prefetch_qbooks
		assert len(books) == 2

	def test_explicit_join(self):
		a = QAuthor.create(name="Alice")
		QBook.create(title="B1", author=a)
		result = QBook.join("author").filter(author__name="Alice").all()
		assert len(result) == 1

	def test_reverse_relation_all(self):
		a = QAuthor.create(name="Alice")
		QBook.create(title="B1", author=a)
		QBook.create(title="B2", author=a)
		books = a.qbooks.all()
		assert len(books) == 2

	def test_reverse_relation_count(self):
		a = QAuthor.create(name="Alice")
		QBook.create(title="B1", author=a)
		assert a.qbooks.count() == 1

	def test_reverse_relation_filter(self):
		a = QAuthor.create(name="Alice")
		QBook.create(title="B1", published=True, author=a)
		QBook.create(title="B2", published=False, author=a)
		result = a.qbooks.filter(published=True).all()
		assert len(result) == 1

	def test_reverse_relation_create(self):
		a = QAuthor.create(name="Alice")
		book = a.qbooks.create(title="New Book")
		assert book.author.id == a.id
		assert QBook.count() == 1


class TestSetOperations:
	def test_union(self):
		QAuthor.create(name="Alice", age=30)
		QAuthor.create(name="Bob", age=25)
		QAuthor.create(name="Carol", age=35)
		qs1 = QAuthor.filter(name="Alice")
		qs2 = QAuthor.filter(name="Bob")
		result = qs1.union(qs2).all()
		assert len(result) == 2

	def test_union_all(self):
		QAuthor.create(name="Alice")
		qs1 = QAuthor.filter(name="Alice")
		qs2 = QAuthor.filter(name="Alice")
		result = qs1.union(qs2, all=True).all()
		assert len(result) == 2

	def test_intersection(self):
		QAuthor.create(name="Alice", age=30)
		QAuthor.create(name="Bob", age=25)
		qs1 = QAuthor.filter(age__gte=25)
		qs2 = QAuthor.filter(name="Alice")
		result = qs1.intersection(qs2).all()
		assert len(result) == 1

	def test_difference(self):
		QAuthor.create(name="Alice", age=30)
		QAuthor.create(name="Bob", age=25)
		qs1 = QAuthor.filter(age__gte=25)
		qs2 = QAuthor.filter(name="Alice")
		result = qs1.difference(qs2).all()
		assert len(result) == 1 and result[0].name == "Bob"


class TestQuerySetChaining:
	def test_chaining_doesnt_mutate_original(self):
		QAuthor.create(name="Alice", age=30)
		QAuthor.create(name="Bob", age=25)
		qs1 = QAuthor.filter(age__gte=20)
		qs2 = qs1.filter(name="Alice")
		assert qs1.count() == 2
		assert qs2.count() == 1

	def test_queryset_is_lazy(self):
		# No error even though table might be empty
		qs = QAuthor.filter(name="test").order_by("name").limit(10)
		sql, params = qs.as_sql()
		assert "SELECT" in sql

	def test_queryset_repr(self):
		qs = QAuthor.filter(name="Alice")
		repr_str = repr(qs)
		assert "QuerySet" in repr_str or "SELECT" in repr_str

	def test_queryset_len(self):
		QAuthor.create(name="A")
		QAuthor.create(name="B")
		qs = QAuthor.filter()
		assert len(qs) == 2

	def test_queryset_iter(self):
		QAuthor.create(name="A")
		QAuthor.create(name="B")
		names = [a.name for a in QAuthor.order_by("name")]
		assert names == ["A", "B"]

	def test_offset_without_limit(self):
		"""Offset without explicit limit should still work (auto LIMIT -1)."""
		for i in range(5):
			QAuthor.create(name=f"u{i}", age=i)
		result = QAuthor.order_by("age").offset(2).all()
		assert len(result) == 3
