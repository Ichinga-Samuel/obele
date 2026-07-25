"""Fluent query builder for the ORM with sync and async APIs.

`QuerySet` instances are returned from `Model.filter()` and friends.
They are lazily evaluated - SQL only runs when results are materialized
(`all()`, `first()`, iteration, slicing, ...).  Async counterparts are
prefixed with `a` (`aall`, `afirst`) and run the query on a worker
thread, so they compose with `async with Database.transaction()`.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, Iterator

from .database import Database, athread, awrite
from .exceptions import MultipleResultsError, RecordNotFoundError
from .fields import ForeignKeyField
from .sql import validate_identifier

if TYPE_CHECKING:
	from .model import Model, ReverseRelationDescriptor

_LOOKUPS: dict[str, str] = {
	"exact": "= ?",
	"ne": "!= ?",
	"gt": "> ?",
	"gte": ">= ?",
	"lt": "< ?",
	"lte": "<= ?",
	"like": "LIKE ?",
	"glob": "GLOB ?",
	"in": "IN",
	"not_in": "NOT_IN",
	"is_null": "IS",
	"between": "BETWEEN",
	"range": "BETWEEN",
	"contains": "CONTAINS",
	"startswith": "STARTSWITH",
	"endswith": "ENDSWITH",
	"iexact": "IEXACT",
	"icontains": "ICONTAINS",
	"istartswith": "ISTARTSWITH",
	"iendswith": "IENDSWITH",
	"regex": "REGEXP ?",
}

_LIKE_ESCAPE_CHAR = "\\"

_LIKE_PATTERNS: dict[str, str] = {
	"contains": "%{v}%",
	"startswith": "{v}%",
	"endswith": "%{v}",
	"icontains": "%{v}%",
	"istartswith": "{v}%",
	"iendswith": "%{v}",
	"iexact": "{v}",
}

def _escape_like(value: str) -> str:
	"""Escape `%`, `_`, and `\\` for LIKE patterns."""
	return (
		value.replace(_LIKE_ESCAPE_CHAR, _LIKE_ESCAPE_CHAR * 2).replace("%", _LIKE_ESCAPE_CHAR + "%").replace("_", _LIKE_ESCAPE_CHAR + "_")
	)


class Q:
	"""Composable boolean query expression.

	Supports `&` (AND), `|` (OR), and `~` (NOT):

	    Q(name="Alice") | Q(name="Bob")
	    ~Q(age__lt=18)
	"""

	__slots__ = ("children", "connector", "negated")

	def __init__(self, *children: Q, **lookups: Any) -> None:
		"""Build a node from child `Q` objects and keyword lookups."""
		self.children: list[Q | tuple[str, Any]] = [*children, *lookups.items()]
		self.connector: str = "AND"
		self.negated: bool = False

	def _combine(self, other: Q, connector: str) -> Q:
		"""Join this node with `other` under the given `connector`."""
		combined = Q(self, other)
		combined.connector = connector
		return combined

	def __and__(self, other: Q) -> Q:
		"""Combine two `Q` objects with `AND`."""
		return self._combine(other, "AND")

	def __or__(self, other: Q) -> Q:
		"""Combine two `Q` objects with `OR`."""
		return self._combine(other, "OR")

	def __invert__(self) -> Q:
		"""Return a negated (`NOT`) copy of this `Q` object."""
		clone = Q()
		clone.children = list(self.children)
		clone.connector = self.connector
		clone.negated = not self.negated
		return clone


class Expression:
	"""Base class for SQL expressions.  Supports arithmetic composition:

	User.filter(score__gt=F("bonus") * 2)
	Post.filter(id__in=[...]).update(views=F("views") + 1)
	"""

	is_aggregate: bool = False

	def as_sql(self, queryset: QuerySet) -> tuple[str, list[Any]]:
		"""Render this expression to `(sql, params)`.  Subclasses must override."""
		raise NotImplementedError

	def _combine(self, other: Any, op: str, reversed_: bool = False) -> CombinedExpression:
		"""Build a `CombinedExpression` of `self` and `other` under `op` (operands swapped when `reversed_`)."""
		return CombinedExpression(other, op, self) if reversed_ else CombinedExpression(self, op, other)

	def __add__(self, other: Any) -> CombinedExpression:
		"""Build a `CombinedExpression` for the `+` operator."""
		return self._combine(other, "+")

	def __radd__(self, other: Any) -> CombinedExpression:
		"""Build a `CombinedExpression` for the reflected `+` operator."""
		return self._combine(other, "+", True)

	def __sub__(self, other: Any) -> CombinedExpression:
		"""Build a `CombinedExpression` for the `-` operator."""
		return self._combine(other, "-")

	def __rsub__(self, other: Any) -> CombinedExpression:
		"""Build a `CombinedExpression` for the reflected `-` operator."""
		return self._combine(other, "-", True)

	def __mul__(self, other: Any) -> CombinedExpression:
		"""Build a `CombinedExpression` for the `*` operator."""
		return self._combine(other, "*")

	def __rmul__(self, other: Any) -> CombinedExpression:
		"""Build a `CombinedExpression` for the reflected `*` operator."""
		return self._combine(other, "*", True)

	def __truediv__(self, other: Any) -> CombinedExpression:
		"""Build a `CombinedExpression` for the `/` operator."""
		return self._combine(other, "/")

	def __rtruediv__(self, other: Any) -> CombinedExpression:
		"""Build a `CombinedExpression` for the reflected `/` operator."""
		return self._combine(other, "/", True)

	def __mod__(self, other: Any) -> CombinedExpression:
		"""Build a `CombinedExpression` for the `%` operator."""
		return self._combine(other, "%")


class CombinedExpression(Expression):
	"""Two expressions joined by an arithmetic operator."""

	__slots__ = ("lhs", "op", "rhs")

	def __init__(self, lhs: Any, op: str, rhs: Any) -> None:
		"""Store the left operand, operator, and right operand."""
		self.lhs = lhs
		self.op = op
		self.rhs = rhs

	def as_sql(self, queryset: QuerySet) -> tuple[str, list[Any]]:
		"""Render the parenthesized `(lhs op rhs)` SQL and combined params."""
		lhs_sql, lhs_params = queryset._coerce_expression(self.lhs).as_sql(queryset)
		rhs_sql, rhs_params = queryset._coerce_expression(self.rhs).as_sql(queryset)
		return f"({lhs_sql} {self.op} {rhs_sql})", [*lhs_params, *rhs_params]


class Value(Expression):
	"""Wrap a literal Python value as an SQL parameter."""

	__slots__ = ("value",)

	def __init__(self, value: Any) -> None:
		"""Store the literal `value` to bind as a parameter."""
		self.value = value

	def as_sql(self, queryset: QuerySet) -> tuple[str, list[Any]]:
		"""Render as a `?` placeholder bound to the wrapped value."""
		return "?", [self.value]


class F(Expression):
	"""Reference a model field (or joined field) by `__`-separated path."""

	__slots__ = ("field_path",)

	def __init__(self, field_path: str) -> None:
		"""Store the `__`-separated `field_path` to reference."""
		self.field_path = field_path

	def as_sql(self, queryset: QuerySet) -> tuple[str, list[Any]]:
		"""Resolve the field path to its `table.column` SQL."""
		column_sql, _ = queryset._resolve_field_reference(self.field_path.split("__"))
		return column_sql, []


class RawSQL(Expression):
	"""Inject raw SQL with optional parameter bindings."""

	__slots__ = ("sql", "params")

	def __init__(self, sql: str, params: list[Any] | tuple[Any, ...] | None = None) -> None:
		"""Store raw `sql` and optional bound `params`."""
		self.sql = sql
		self.params = list(params or [])

	def as_sql(self, queryset: QuerySet) -> tuple[str, list[Any]]:
		"""Return the stored raw SQL and its parameters."""
		return self.sql, list(self.params)


class Func(Expression):
	"""Call an SQL function with arguments."""

	__slots__ = ("name", "args", "is_aggregate")

	def __init__(self, name: str, *args: Any, is_aggregate: bool = False) -> None:
		"""Store the function `name`, positional `args`, and aggregate flag."""
		self.name = name
		self.args = args
		self.is_aggregate = is_aggregate

	def as_sql(self, queryset: QuerySet) -> tuple[str, list[Any]]:
		"""Render the function call with its comma-separated arguments."""
		sql_parts: list[str] = []
		params: list[Any] = []
		for arg in self.args:
			arg_sql, arg_params = queryset._coerce_expression(arg).as_sql(queryset)
			sql_parts.append(arg_sql)
			params.extend(arg_params)
		return f"{self.name}({', '.join(sql_parts)})", params


class Count(Func):
	"""`COUNT` aggregate function expression."""

	def __init__(self, *args: Any) -> None:
		"""Build a `COUNT` over `args` (defaults to `COUNT(*)`)."""
		super().__init__("COUNT", *(args or ("*",)), is_aggregate=True)


class Sum(Func):
	"""`SUM` aggregate function expression."""

	def __init__(self, *args: Any) -> None:
		"""Build a `SUM` over `args`."""
		super().__init__("SUM", *args, is_aggregate=True)


class Avg(Func):
	"""`AVG` aggregate function expression."""

	def __init__(self, *args: Any) -> None:
		"""Build an `AVG` over `args`."""
		super().__init__("AVG", *args, is_aggregate=True)


class Min(Func):
	"""`MIN` aggregate function expression."""

	def __init__(self, *args: Any) -> None:
		"""Build a `MIN` over `args`."""
		super().__init__("MIN", *args, is_aggregate=True)


class Max(Func):
	"""`MAX` aggregate function expression."""

	def __init__(self, *args: Any) -> None:
		"""Build a `MAX` over `args`."""
		super().__init__("MAX", *args, is_aggregate=True)


class Subquery(Expression):
	"""Embed another QuerySet as a subquery."""

	__slots__ = ("queryset", "field")

	def __init__(self, queryset: QuerySet, field: str | None = None) -> None:
		"""Store the `queryset` and optional `field` to select."""
		self.queryset = queryset
		self.field = field

	def as_sql(self, queryset: QuerySet) -> tuple[str, list[Any]]:
		"""Render the wrapped `QuerySet` as a single-column `SELECT`."""
		field_name = self.field or self.queryset.model_cls._pk_name
		column_sql, _ = self.queryset._resolve_field_reference(field_name.split("__"))
		return self.queryset._build_select(select_override=[column_sql], include_annotations=False)


_STAR = RawSQL("*")


@dataclass(slots=True)
class _JoinSpec:
	"""Resolved metadata for a single SQL `JOIN` in a `QuerySet`."""

	path: tuple[str, ...]
	alias: str
	related_model: type[Model]
	relation_name: str
	relation_kind: str
	relation_field_name: str
	sql: str


class QuerySet:
	"""Lazy, chainable SQL query builder with sync and async APIs.

	Every chaining method returns a **copy**, so partial queries can be
	reused.  Async methods are prefixed with `a` (`aall`, `afirst`).
	"""

	def __init__(self, model_cls: type[Model]) -> None:
		"""Initialize an empty query builder bound to `model_cls`."""
		self.model_cls = model_cls
		self._where_fragments: list[tuple[str, list[Any]]] = []
		self._order_fields: list[str] = []
		self._limit_val: int | None = None
		self._offset_val: int | None = None
		self._join_specs: list[_JoinSpec] = []
		self._join_map: dict[tuple[str, ...], _JoinSpec] = {}
		self._select_fields: list[str] = [f"{model_cls.table_name}.*"]
		self._selected_related: dict[str, _JoinSpec] = {}
		self._annotations: dict[str, Expression] = {}
		self._distinct: bool = False
		self._group_by_fields: list[str] = []
		self._having_fragments: list[tuple[str, list[Any]]] = []
		self._values_fields: list[str] | None = None
		self._values_mode: str | None = None
		self._values_flat: bool = False
		self._only_fields: list[str] | None = None
		self._defer_fields: list[str] | None = None
		self._prefetch_relations: list[str] = []
		self._raw_sql: str | None = None
		self._raw_params: list[Any] = []

	def __iter__(self) -> Iterator[Any]:
		"""Iterate results, streaming unless relations are prefetched."""
		if self._prefetch_relations:
			return iter(self.all())
		return self.iterator()

	async def __aiter__(self) -> AsyncIterator[Any]:
		"""Async version of `__iter__`."""
		if self._prefetch_relations:
			for item in await self.aall():
				yield item
			return
		async for item in self.aiterator():
			yield item

	def __len__(self) -> int:
		"""Return the number of matching rows (delegates to `count`)."""
		return self.count()

	def __bool__(self) -> bool:
		"""Return whether any row matches (delegates to `exists`)."""
		return self.exists()

	def __getitem__(self, item: int | slice) -> Any:
		"""Support `qs[i]` and lazy `qs[start:stop]` slicing."""
		if isinstance(item, slice):
			if item.step is not None:
				raise ValueError("QuerySet slicing does not support a step")
			start = item.start or 0
			if start < 0 or (item.stop is not None and item.stop < 0):
				raise ValueError("QuerySet slicing does not support negative indexes")
			qs = self._clone()
			qs._offset_val = (self._offset_val or 0) + start if start else self._offset_val
			if item.stop is not None:
				qs._limit_val = max(0, item.stop - start)
			return qs
		if item < 0:
			raise ValueError("QuerySet indexing does not support negative indexes")
		qs = self._clone()
		qs._offset_val = (self._offset_val or 0) + item
		qs._limit_val = 1
		results = qs.all()
		if not results:
			raise IndexError("QuerySet index out of range")
		return results[0]

	def __repr__(self) -> str:
		"""Return a debug representation showing the built SQL and params."""
		sql, params = self._build_select()
		return f"<QuerySet sql={sql!r} params={params!r}>"

	def _clone(self) -> QuerySet:
		"""Return a copy of this `QuerySet` so chaining never mutates in place."""
		qs = QuerySet.__new__(QuerySet)
		qs.model_cls = self.model_cls
		qs._where_fragments = list(self._where_fragments)
		qs._order_fields = list(self._order_fields)
		qs._limit_val = self._limit_val
		qs._offset_val = self._offset_val
		qs._join_specs = list(self._join_specs)
		qs._join_map = dict(self._join_map)
		qs._select_fields = list(self._select_fields)
		qs._selected_related = dict(self._selected_related)
		qs._annotations = dict(self._annotations)
		qs._distinct = self._distinct
		qs._group_by_fields = list(self._group_by_fields)
		qs._having_fragments = list(self._having_fragments)
		qs._values_fields = list(self._values_fields) if self._values_fields is not None else None
		qs._values_mode = self._values_mode
		qs._values_flat = self._values_flat
		qs._only_fields = list(self._only_fields) if self._only_fields is not None else None
		qs._defer_fields = list(self._defer_fields) if self._defer_fields is not None else None
		qs._prefetch_relations = list(self._prefetch_relations)
		qs._raw_sql = self._raw_sql
		qs._raw_params = list(self._raw_params)
		return qs

	def _assert_composable(self, operation: str) -> None:
		"""Raise `ValueError` if `operation` follows a set operation (union/intersection/difference)."""
		if self._raw_sql is not None:
			raise ValueError(f"{operation} is not supported after a set operation (union/intersection/difference)")

	def filter(self, *conditions: Q, **kwargs: Any) -> QuerySet:
		"""Add `WHERE` conditions (AND logic).

		Accepts keyword lookups and/or `Q` objects:

		    User.filter(name="Alice", age__gt=18)
		    User.filter(Q(name="Alice") | Q(name="Bob"))
		"""
		self._assert_composable("filter()")
		qs = self._clone()
		if kwargs:
			conditions = (*conditions, Q(**kwargs))
		for condition in conditions:
			if not isinstance(condition, Q):
				raise TypeError("filter() positional arguments must be Q objects")
			qs._where_fragments.append(qs._compile_q(condition))
		return qs

	def exclude(self, *conditions: Q, **kwargs: Any) -> QuerySet:
		"""Add negated `WHERE` conditions."""
		self._assert_composable("exclude()")
		qs = self._clone()
		if kwargs:
			conditions = (*conditions, Q(**kwargs))
		for condition in conditions:
			if not isinstance(condition, Q):
				raise TypeError("exclude() positional arguments must be Q objects")
			qs._where_fragments.append(qs._compile_q(~condition))
		return qs

	def order_by(self, *fields: str) -> QuerySet:
		"""Add `ORDER BY` columns; prefix a name with `-` for descending."""
		qs = self._clone()
		for field_name in fields:
			direction = "DESC" if field_name.startswith("-") else "ASC"
			raw_name = field_name.lstrip("-")
			if raw_name in qs._annotations or qs._raw_sql is not None:
				validate_identifier(raw_name, kind="order field")
				qs._order_fields.append(f"{raw_name} {direction}")
				continue
			column_sql, _ = qs._resolve_field_reference(raw_name.split("__"))
			qs._order_fields.append(f"{column_sql} {direction}")
		return qs

	def limit(self, n: int) -> QuerySet:
		"""Cap the result set at `n` rows (`LIMIT`)."""
		if n < 0:
			raise ValueError("limit() cannot be negative")
		qs = self._clone()
		qs._limit_val = n
		return qs

	def offset(self, n: int) -> QuerySet:
		"""Skip the first `n` rows (`OFFSET`)."""
		if n < 0:
			raise ValueError("offset() cannot be negative")
		qs = self._clone()
		qs._offset_val = n
		return qs

	def distinct(self) -> QuerySet:
		"""Add `SELECT DISTINCT`."""
		qs = self._clone()
		qs._distinct = True
		return qs

	def values(self, *fields: str) -> QuerySet:
		"""Return dicts of the given fields instead of model instances."""
		self._assert_composable("values()")
		qs = self._clone()
		qs._values_fields = list(fields) if fields else list(self.model_cls._fields.keys())
		qs._values_mode = "dict"
		qs._values_flat = False
		return qs

	def values_list(self, *fields: str, flat: bool = False) -> QuerySet:
		"""Return tuples (or a flat list when `flat=True`) of the given fields."""
		if flat and len(fields) != 1:
			raise ValueError("flat=True requires exactly one field")
		self._assert_composable("values_list()")
		qs = self._clone()
		qs._values_fields = list(fields) if fields else list(self.model_cls._fields.keys())
		qs._values_mode = "tuple"
		qs._values_flat = flat
		return qs

	def only(self, *fields: str) -> QuerySet:
		"""Load only the given fields (plus the PK); others come back `None`."""
		self._assert_composable("only()")
		unknown = [f for f in fields if f not in self.model_cls._fields]
		if unknown:
			raise ValueError(f"Unknown field(s) in only(): {unknown!r}")
		qs = self._clone()
		qs._only_fields = list(fields)
		return qs

	def defer(self, *fields: str) -> QuerySet:
		"""Skip loading the given fields; they come back `None`."""
		self._assert_composable("defer()")
		unknown = [f for f in fields if f not in self.model_cls._fields]
		if unknown:
			raise ValueError(f"Unknown field(s) in defer(): {unknown!r}")
		qs = self._clone()
		qs._defer_fields = list(fields)
		return qs

	def join(self, relation_name: str, *, join_type: str = "INNER") -> QuerySet:
		"""Explicitly join on a relation (forward FK or reverse)."""
		self._assert_composable("join()")
		qs = self._clone()
		qs._ensure_join(tuple(relation_name.split("__")), join_type=join_type)
		return qs

	def select_related(self, *fk_fields: str) -> QuerySet:
		"""Eagerly join on ForeignKeyFields and hydrate the related objects."""
		self._assert_composable("select_related()")
		qs = self._clone()
		for fk_name in fk_fields:
			if "__" in fk_name:
				raise ValueError("select_related() currently supports only direct foreign keys")
			field_obj = qs.model_cls._fields.get(fk_name)
			if not isinstance(field_obj, ForeignKeyField):
				raise ValueError(f"'{fk_name}' is not a ForeignKeyField")
			join_spec = qs._ensure_join((fk_name,), join_type="LEFT")
			qs._selected_related[fk_name] = join_spec
			for field in join_spec.related_model._fields.values():
				qs._select_fields.append(f"{join_spec.alias}.{field.column_name} AS {fk_name}__{field.column_name}")
		return qs

	def prefetch_related(self, *relations: str) -> QuerySet:
		"""Batch-load reverse FK relations in one extra query per relation.

		Unlike `select_related` (JOIN-based), this avoids row duplication.
		Prefetched managers serve cached rows:

		    users = User.prefetch_related("posts").all()
		    for user in users:
		        user.posts.all()   # no query
		"""
		self._assert_composable("prefetch_related()")
		qs = self._clone()
		qs._prefetch_relations = list(dict.fromkeys(qs._prefetch_relations + list(relations)))
		return qs

	def annotate(self, **annotations: Any) -> QuerySet:
		"""Add computed columns via expressions:

		User.annotate(post_count=Count("posts__id"))
		"""
		self._assert_composable("annotate()")
		qs = self._clone()
		for alias, expression in annotations.items():
			validate_identifier(alias, kind="annotation alias")
			qs._annotations[alias] = qs._coerce_expression(expression)
		return qs

	def group_by(self, *fields: str) -> QuerySet:
		"""Add explicit GROUP BY columns."""
		self._assert_composable("group_by()")
		qs = self._clone()
		for field_name in fields:
			col, _ = qs._resolve_field_reference(field_name.split("__"))
			qs._group_by_fields.append(col)
		return qs

	def having(self, *conditions: Q, **kwargs: Any) -> QuerySet:
		"""Add HAVING conditions for filtered aggregates."""
		self._assert_composable("having()")
		qs = self._clone()
		if kwargs:
			conditions = (*conditions, Q(**kwargs))
		for condition in conditions:
			if not isinstance(condition, Q):
				raise TypeError("having() positional arguments must be Q objects")
			qs._having_fragments.append(qs._compile_q(condition))
		return qs

	def union(self, other: QuerySet, *, all: bool = False) -> QuerySet:
		"""Combine with another QuerySet using UNION (or UNION ALL)."""
		return self._set_operation("UNION ALL" if all else "UNION", other)

	def intersection(self, other: QuerySet) -> QuerySet:
		"""Combine with another QuerySet using INTERSECT."""
		return self._set_operation("INTERSECT", other)

	def difference(self, other: QuerySet) -> QuerySet:
		"""Combine with another QuerySet using EXCEPT."""
		return self._set_operation("EXCEPT", other)

	def _set_operation(self, op: str, other: QuerySet) -> QuerySet:
		"""Build a raw combined `QuerySet` joining two selects with `op` (UNION/INTERSECT/EXCEPT)."""
		left_sql, left_params = self._build_select()
		right_sql, right_params = other._build_select()
		qs = QuerySet(self.model_cls)
		qs._values_fields = self._values_fields
		qs._values_mode = self._values_mode
		qs._values_flat = self._values_flat
		qs._raw_sql = f"{left_sql} {op} {right_sql}"
		qs._raw_params = left_params + right_params
		return qs

	def _get_select_columns(self) -> list[str]:
		"""Compute the `SELECT` column list honoring `only`/`defer`/`values`."""
		model = self.model_cls
		table = model.table_name
		if self._only_fields is not None:
			names = dict.fromkeys([*self._only_fields, model._pk_name])
			return [f"{table}.{model._fields[n].column_name}" for n in names]
		if self._defer_fields is not None:
			deferred = set(self._defer_fields)
			return [f"{table}.{f.column_name}" for n, f in model._fields.items() if n not in deferred]
		if self._values_fields is not None:
			cols = []
			for name in self._values_fields:
				if name in model._fields:
					cols.append(f"{table}.{model._fields[name].column_name}")
				elif name not in self._annotations:
					raise ValueError(f"Unknown field or annotation {name!r}")
			return cols
		return list(self._select_fields)

	def _build_select(self, *, select_override: list[str] | None = None, include_annotations: bool = True) -> tuple[str, list[Any]]:
		"""Assemble the full `SELECT` statement and its parameters."""
		if self._raw_sql is not None:
			sql, params = self._raw_sql, list(self._raw_params)
			if self._order_fields:
				sql += f" ORDER BY {', '.join(self._order_fields)}"
			sql += self._limit_offset_sql()
			return sql, params

		params: list[Any] = []
		select_parts = list(select_override or self._get_select_columns())
		if include_annotations:
			for alias, expression in self._annotations.items():
				expr_sql, expr_params = expression.as_sql(self)
				select_parts.append(f"{expr_sql} AS {alias}")
				params.extend(expr_params)

		distinct = "DISTINCT " if self._distinct else ""
		parts = [f"SELECT {distinct}{', '.join(select_parts)}", "FROM", self.model_cls.table_name]
		parts.extend(join_spec.sql for join_spec in self._join_specs)

		if self._where_fragments:
			parts.append("WHERE " + " AND ".join(frag for frag, _ in self._where_fragments))
			for _, frag_params in self._where_fragments:
				params.extend(frag_params)

		group_by_cols = list(self._group_by_fields)
		if not group_by_cols and include_annotations and any(expr.is_aggregate for expr in self._annotations.values()):
			group_by_cols.append(f"{self.model_cls.table_name}.{self.model_cls._pk_field.column_name}")
		if group_by_cols:
			parts.append(f"GROUP BY {', '.join(group_by_cols)}")

		if self._having_fragments:
			parts.append("HAVING " + " AND ".join(frag for frag, _ in self._having_fragments))
			for _, frag_params in self._having_fragments:
				params.extend(frag_params)

		if self._order_fields:
			parts.append(f"ORDER BY {', '.join(self._order_fields)}")
		return " ".join(parts) + self._limit_offset_sql(), params

	def _limit_offset_sql(self) -> str:
		"""Render the trailing `LIMIT`/`OFFSET` clause."""
		sql = ""
		if self._limit_val is not None:
			sql += f" LIMIT {self._limit_val}"
		elif self._offset_val is not None:
			sql += " LIMIT -1"
		if self._offset_val is not None:
			sql += f" OFFSET {self._offset_val}"
		return sql

	def as_sql(self) -> tuple[str, list[Any]]:
		"""Return the `(sql, params)` pair without executing."""
		return self._build_select()

	def explain(self) -> str:
		"""Return `EXPLAIN QUERY PLAN` output for debugging."""
		sql, params = self._build_select()
		rows = Database.fetchall(f"EXPLAIN QUERY PLAN {sql}", params)
		return "\n".join(f"  {row['detail']}" if "detail" in row.keys() else str(dict(row)) for row in rows)

	def _row_to_instance(self, row: Any) -> Model:
		"""Hydrate a DB `row` into a model instance, including selected relations."""
		row_dict = dict(row)
		annotations = {alias: row_dict[alias] for alias in self._annotations if alias in row_dict}
		instance = self.model_cls._from_row(row_dict, annotations=annotations)

		for relation_name, join_spec in self._selected_related.items():
			related_row = {}
			for field in join_spec.related_model._fields.values():
				alias_name = f"{relation_name}__{field.column_name}"
				if alias_name in row_dict:
					related_row[field.column_name] = row_dict[alias_name]
			if not related_row or all(v is None for v in related_row.values()):
				continue
			related_instance = join_spec.related_model._from_row(related_row)
			fk_field = self.model_cls._fields[relation_name]
			instance.__dict__[fk_field.cache_attr_name] = related_instance
		return instance

	def _row_to_values(self, row: Any) -> Any:
		"""Convert a DB `row` into a dict, tuple, or flat value per `values` mode."""
		row_dict = dict(row)
		fields = self._values_fields

		def value_of(name: str) -> Any:
			"""Look up `name` in the row by column name, then by raw key."""
			field = self.model_cls._fields.get(name)
			if field is not None and field.column_name in row_dict:
				return row_dict[field.column_name]
			return row_dict.get(name)

		if self._values_flat:
			return value_of(fields[0])
		if self._values_mode == "tuple":
			return tuple(value_of(name) for name in fields)
		return {name: value_of(name) for name in fields}

	def _converter(self) -> Any:
		"""Return the row-conversion callable for the current `values` mode."""
		return self._row_to_values if self._values_fields is not None else self._row_to_instance

	def all(self) -> list:
		"""Execute the query and return all results."""
		results = list(self.iterator())
		if self._prefetch_relations:
			self._do_prefetch(results)
		return results

	async def aall(self) -> list:
		"""Async version of `all`."""
		return await athread(self.all)

	def first(self) -> Any:
		"""Return the first result or `None`."""
		for item in self.limit(1).iterator():
			return item
		return None

	async def afirst(self) -> Any:
		"""Async version of `first`."""
		return await athread(self.first)

	def last(self) -> Any:
		"""Return the last result (reversed ordering; PK order when unordered)."""
		return self._reversed().first()

	async def alast(self) -> Any:
		"""Async version of `last`."""
		return await athread(self.last)

	def _reversed(self) -> QuerySet:
		"""Return a copy with ordering reversed (PK order when unordered)."""
		qs = self._clone()
		if qs._order_fields:
			qs._order_fields = [f"{f[:-4]} DESC" if f.endswith(" ASC") else f"{f[:-5]} ASC" for f in qs._order_fields]
			return qs
		return qs.order_by(f"-{self.model_cls._pk_name}")

	def get(self, **kwargs: Any) -> Model:
		"""Return exactly one result; raises on zero or multiple matches."""
		qs = self.filter(**kwargs) if kwargs else self._clone()
		qs._limit_val = 2
		results = list(qs.iterator())
		if not results:
			raise RecordNotFoundError(f"No {self.model_cls.__name__} matches the given query")
		if len(results) > 1:
			raise MultipleResultsError(f"Expected 1 {self.model_cls.__name__}, got multiple")
		return results[0]

	async def aget(self, **kwargs: Any) -> Model:
		"""Async version of `get`."""
		return await athread(functools.partial(self.get, **kwargs))

	def latest(self, field: str | None = None) -> Model:
		"""Return the newest row by `field` (default: PK).  Raises if empty."""
		name = field or self.model_cls._pk_name
		result = self.order_by(f"-{name}").first()
		if result is None:
			raise RecordNotFoundError(f"No {self.model_cls.__name__} matches the given query")
		return result

	async def alatest(self, field: str | None = None) -> Model:
		"""Async version of `latest`."""
		return await athread(self.latest, field)

	def earliest(self, field: str | None = None) -> Model:
		"""Return the oldest row by `field` (default: PK).  Raises if empty."""
		name = field or self.model_cls._pk_name
		result = self.order_by(name).first()
		if result is None:
			raise RecordNotFoundError(f"No {self.model_cls.__name__} matches the given query")
		return result

	async def aearliest(self, field: str | None = None) -> Model:
		"""Async version of `earliest`."""
		return await athread(self.earliest, field)

	def in_bulk(self, values: Any = None, *, field: str | None = None) -> dict[Any, Model]:
		"""Return `{key: instance}` for the given key `values` (default: all rows).

		`field` defaults to the primary key and must be unique per row.
		"""
		name = field or self.model_cls._pk_name
		qs = self.filter(**{f"{name}__in": list(values)}) if values is not None else self
		return {instance.__dict__.get(name): instance for instance in qs.iterator()}

	async def ain_bulk(self, values: Any = None, *, field: str | None = None) -> dict[Any, Model]:
		"""Async version of `in_bulk`."""
		return await athread(functools.partial(self.in_bulk, values, field=field))

	def iterator(self, chunk_size: int = 2000) -> Iterator:
		"""Stream results without materializing the full list."""
		sql, params = self._build_select()
		cursor = Database.execute_read(sql, params)
		converter = self._converter()
		while True:
			rows = cursor.fetchmany(chunk_size)
			if not rows:
				return
			for row in rows:
				yield converter(row)

	async def aiterator(self, chunk_size: int = 2000) -> AsyncIterator:
		"""Async streaming iteration (chunked fetches on a worker thread)."""
		sql, params = self._build_select()
		cursor = await athread(Database.execute_read, sql, params)
		converter = self._converter()
		while True:
			rows = await athread(cursor.fetchmany, chunk_size)
			if not rows:
				return
			for row in rows:
				yield converter(row)

	def count(self) -> int:
		"""Return the number of matching rows."""
		sql, params = self._build_select()
		return int(Database.fetch_value(f"SELECT COUNT(*) AS cnt FROM ({sql})", params, column="cnt") or 0)

	async def acount(self) -> int:
		"""Async version of `count`."""
		return await athread(self.count)

	def exists(self) -> bool:
		"""Return `True` if at least one row matches (`SELECT 1 ... LIMIT 1`)."""
		sql, params = self._build_select()
		return Database.fetchone(f"SELECT 1 FROM ({sql}) LIMIT 1", params) is not None

	async def aexists(self) -> bool:
		"""Async version of `exists`."""
		return await athread(self.exists)

	def aggregate(self, func: str, field: str) -> Any:
		"""Run an aggregate function (SUM, AVG, MIN, MAX, COUNT) over `field`."""
		func = func.upper()
		if func not in ("SUM", "AVG", "MIN", "MAX", "COUNT"):
			raise ValueError(f"Unsupported aggregate function: {func}")
		col, _ = self._resolve_field_reference(field.split("__"))
		base_sql, params = self._build_select(select_override=[f"{col} AS __agg_target__"], include_annotations=False)
		return Database.fetch_value(f"SELECT {func}(__agg_target__) AS result FROM ({base_sql})", params, column="result")

	async def aaggregate(self, func: str, field: str) -> Any:
		"""Async version of `aggregate`."""
		return await athread(self.aggregate, func, field)

	def paginate(self, *, page: int = 1, per_page: int = 20) -> Any:
		"""Return an offset-based `Page`."""
		from .pagination import paginate_queryset

		return paginate_queryset(self, page=page, per_page=per_page)

	async def apaginate(self, *, page: int = 1, per_page: int = 20) -> Any:
		"""Async version of `paginate`."""
		return await athread(functools.partial(self.paginate, page=page, per_page=per_page))

	def cursor_paginate(self, *, per_page: int = 20, cursor_field: str = "", after: Any = None, before: Any = None) -> Any:
		"""Return a cursor-based `CursorPage`."""
		from .pagination import cursor_paginate_queryset

		return cursor_paginate_queryset(self, per_page=per_page, cursor_field=cursor_field, after=after, before=before)

	async def acursor_paginate(self, **kwargs: Any) -> Any:
		"""Async version of `cursor_paginate`."""
		return await athread(functools.partial(self.cursor_paginate, **kwargs))

	def _do_prefetch(self, instances: list) -> None:
		"""Batch-load and attach prefetched reverse relations onto `instances`."""
		if not instances:
			return
		for relation_name in self._prefetch_relations:
			descriptor = getattr(self.model_cls, "_reverse_relations", {}).get(relation_name)
			if descriptor is None:
				raise ValueError(f"'{relation_name}' is not a known reverse relation on {self.model_cls.__name__}")
			fk_field_name = descriptor.field_name
			pk_values = [inst.pk for inst in instances if inst.pk is not None]
			if not pk_values:
				continue
			related_items = descriptor.related_model.filter(**{f"{fk_field_name}__in": pk_values}).all()
			grouped: dict[Any, list] = {}
			for item in related_items:
				grouped.setdefault(item.__dict__.get(fk_field_name), []).append(item)
			for inst in instances:
				inst.__dict__[f"_prefetch_{relation_name}"] = grouped.get(inst.pk, [])

	def update(self, *, validate: bool = True, **kwargs: Any) -> int:
		"""Bulk UPDATE matching rows; supports expressions.  Returns affected count:

		Post.filter(id=pk).update(views=F("views") + 1)
		"""
		if not kwargs:
			raise ValueError("update() requires at least one field")
		self._assert_composable("update()")
		if self._join_specs:
			raise ValueError("update() does not support joined filters")
		set_parts: list[str] = []
		set_params: list[Any] = []
		for key, value in kwargs.items():
			field_obj = self.model_cls._fields.get(key)
			col = field_obj.column_name if field_obj else key
			if isinstance(value, (Expression, QuerySet)):
				expr_sql, expr_params = self._coerce_expression(value).as_sql(self)
				set_parts.append(f"{col} = {expr_sql}")
				set_params.extend(expr_params)
				continue
			if validate and field_obj:
				field_obj.validate(value)
			set_parts.append(f"{col} = ?")
			set_params.append(field_obj.to_db(value) if field_obj else value)
		if self._join_specs:
			raise ValueError("update() expressions cannot reference joined relations")

		sql = f"UPDATE {self.model_cls.table_name} SET {', '.join(set_parts)}"
		where_params: list[Any] = []
		if self._where_fragments:
			sql += " WHERE " + " AND ".join(frag for frag, _ in self._where_fragments)
			for _, frag_params in self._where_fragments:
				where_params.extend(frag_params)
		return Database.execute(sql, set_params + where_params).rowcount

	async def aupdate(self, *, validate: bool = True, **kwargs: Any) -> int:
		"""Async version of `update`."""
		return await awrite(functools.partial(self.update, validate=validate, **kwargs))

	def delete(self) -> int:
		"""Bulk DELETE matching rows.  Returns the number of rows removed."""
		self._assert_composable("delete()")
		if self._join_specs:
			raise ValueError("delete() does not support joined filters")
		sql = f"DELETE FROM {self.model_cls.table_name}"
		params: list[Any] = []
		if self._where_fragments:
			sql += " WHERE " + " AND ".join(frag for frag, _ in self._where_fragments)
			for _, frag_params in self._where_fragments:
				params.extend(frag_params)
		return Database.execute(sql, params).rowcount

	async def adelete(self) -> int:
		"""Async version of `delete`."""
		return await awrite(self.delete)

	def _split_lookup(self, key: str) -> tuple[list[str], str]:
		"""Split a lookup `key` into its field path and lookup operator (default `exact`)."""
		parts = key.split("__")
		if len(parts) > 1 and parts[-1] in _LOOKUPS:
			return parts[:-1], parts[-1]
		return parts, "exact"

	def _compile_q(self, expression: Q) -> tuple[str, list[Any]]:
		"""Compile a `Q` tree into an SQL fragment and its parameters."""
		compiled_children: list[str] = []
		params: list[Any] = []
		for child in expression.children:
			if isinstance(child, Q):
				child_sql, child_params = self._compile_q(child)
			else:
				child_sql, child_params = self._compile_condition(*child)
			compiled_children.append(f"({child_sql})")
			params.extend(child_params)

		compiled_sql = f" {expression.connector} ".join(compiled_children) if compiled_children else "1=1"
		if expression.negated:
			compiled_sql = f"NOT ({compiled_sql})"
		return compiled_sql, params

	def _compile_condition(self, key: str, value: Any) -> tuple[str, list[Any]]:
		"""Compile a single `field__lookup=value` condition to SQL and params."""
		field_parts, lookup = self._split_lookup(key)
		if len(field_parts) == 1 and field_parts[0] in self._annotations:
			column_sql = field_parts[0]
			field_obj = None
		else:
			column_sql, field_obj = self._resolve_field_reference(field_parts)

		handler = _LOOKUP_DISPATCH.get(lookup)
		if handler is not None:
			return handler(self, column_sql, field_obj, value)

		if lookup in _LIKE_PATTERNS:
			return self._compile_like(column_sql, lookup, value)

		if value is None and lookup in ("exact", "ne"):
			return f"{column_sql} IS {'NOT ' if lookup == 'ne' else ''}NULL", []

		if isinstance(value, QuerySet):
			value = Subquery(value)
		if isinstance(value, Subquery):
			sub_sql, sub_params = value.as_sql(self)
			return f"{column_sql} {_LOOKUPS[lookup].replace('?', f'({sub_sql})')}", sub_params
		if isinstance(value, Expression):
			expr_sql, expr_params = value.as_sql(self)
			return f"{column_sql} {_LOOKUPS[lookup].replace('?', expr_sql)}", expr_params

		db_val = field_obj.to_db(value) if field_obj else value
		return f"{column_sql} {_LOOKUPS[lookup]}", [db_val]

	@staticmethod
	def _compile_like(column_sql: str, lookup: str, value: Any) -> tuple[str, list[Any]]:
		"""Compile `contains`/`startswith`/`endswith` (and case-insensitive variants) to `LIKE` SQL."""
		pattern = _LIKE_PATTERNS[lookup].replace("{v}", _escape_like(str(value)))
		if lookup.startswith("i"):
			return f"LOWER({column_sql}) LIKE ? ESCAPE '\\'", [pattern.lower()]
		return f"{column_sql} LIKE ? ESCAPE '\\'", [pattern]

	def _coerce_expression(self, value: Any) -> Expression:
		"""Wrap `value` as an `Expression` (field reference, subquery, or literal)."""
		if isinstance(value, Expression):
			return value
		if isinstance(value, QuerySet):
			return Subquery(value)
		if isinstance(value, str):
			if value == "*":
				return _STAR
			if "__" in value or value in self.model_cls._fields:
				return F(value)
		return Value(value)

	def _resolve_field_reference(self, path_parts: list[str]) -> tuple[str, Any]:
		"""Resolve a `__`-separated path to `(table.column, field_obj)`."""
		if not path_parts:
			raise ValueError("field path cannot be empty")

		current_model = self.model_cls
		current_alias = self.model_cls.table_name
		for idx in range(1, len(path_parts)):
			join_spec = self._ensure_join(tuple(path_parts[:idx]), join_type="LEFT")
			current_model = join_spec.related_model
			current_alias = join_spec.alias

		field_name = path_parts[-1]
		field_obj = current_model._fields.get(field_name)
		if field_obj is None:
			raise ValueError(f"Unknown field path '{'__'.join(path_parts)}' for {self.model_cls.__name__}")
		return f"{current_alias}.{field_obj.column_name}", field_obj

	def _ensure_join(self, path: tuple[str, ...], *, join_type: str = "LEFT") -> _JoinSpec:
		"""Return (creating and caching if needed) the `_JoinSpec` for relation `path`."""
		existing = self._join_map.get(path)
		if existing is not None:
			return existing

		parent_model = self.model_cls
		parent_alias = self.model_cls.table_name
		if len(path) > 1:
			parent_join = self._ensure_join(path[:-1], join_type=join_type)
			parent_model = parent_join.related_model
			parent_alias = parent_join.alias

		relation_name = path[-1]
		alias = f"jt{len(self._join_specs)}"

		if relation_name in parent_model._fields:
			field_obj = parent_model._fields[relation_name]
			if not isinstance(field_obj, ForeignKeyField):
				raise ValueError(f"'{relation_name}' is not a joinable ForeignKeyField")
			related_model = field_obj.related_model
			sql = (
				f"{join_type} JOIN {related_model.table_name} AS {alias} "
				f"ON {parent_alias}.{field_obj.column_name} = {alias}.{related_model._pk_field.column_name}"
			)
			join_spec = _JoinSpec(
				path=path,
				alias=alias,
				related_model=related_model,
				relation_name=relation_name,
				relation_kind="forward",
				relation_field_name=relation_name,
				sql=sql,
			)
		else:
			descriptor: ReverseRelationDescriptor | None = getattr(parent_model, "_reverse_relations", {}).get(relation_name)
			if descriptor is None:
				raise ValueError(f"'{relation_name}' is not a joinable relation on {parent_model.__name__}")
			related_model = descriptor.related_model
			fk_field = related_model._fields[descriptor.field_name]
			sql = (
				f"{join_type} JOIN {related_model.table_name} AS {alias} "
				f"ON {alias}.{fk_field.column_name} = {parent_alias}.{parent_model._pk_field.column_name}"
			)
			join_spec = _JoinSpec(
				path=path,
				alias=alias,
				related_model=related_model,
				relation_name=relation_name,
				relation_kind="reverse",
				relation_field_name=descriptor.field_name,
				sql=sql,
			)

		self._join_map[path] = join_spec
		self._join_specs.append(join_spec)
		return join_spec

def _compile_in(qs: QuerySet, col: str, field_obj: Any, value: Any) -> tuple[str, list[Any]]:
	"""Compile an `in` lookup, supporting subqueries and iterables of values."""
	if isinstance(value, QuerySet):
		value = Subquery(value)
	if isinstance(value, Subquery):
		sub_sql, sub_params = value.as_sql(qs)
		return f"{col} IN ({sub_sql})", sub_params
	values = list(value) if isinstance(value, (list, tuple, set, frozenset)) else [value]
	if not values:
		return "0=1", []
	placeholders = ", ".join("?" for _ in values)
	params = [field_obj.to_db(v) if field_obj else v for v in values]
	return f"{col} IN ({placeholders})", params


def _compile_not_in(qs: QuerySet, col: str, field_obj: Any, value: Any) -> tuple[str, list[Any]]:
	"""Compile a `not_in` lookup against an iterable of values."""
	values = list(value) if isinstance(value, (list, tuple, set, frozenset)) else [value]
	if not values:
		return "1=1", []
	placeholders = ", ".join("?" for _ in values)
	params = [field_obj.to_db(v) if field_obj else v for v in values]
	return f"{col} NOT IN ({placeholders})", params


def _compile_is_null(qs: QuerySet, col: str, field_obj: Any, value: Any) -> tuple[str, list[Any]]:
	"""Compile an `is_null` lookup to `IS NULL` or `IS NOT NULL`."""
	return f"{col} {'IS NULL' if value else 'IS NOT NULL'}", []


def _compile_between(qs: QuerySet, col: str, field_obj: Any, value: Any) -> tuple[str, list[Any]]:
	"""Compile a `between`/`range` lookup to `BETWEEN ? AND ?` from a 2-element value."""
	if not isinstance(value, (list, tuple)) or len(value) != 2:
		raise ValueError("'between' lookup requires a 2-element tuple/list")
	lo, hi = value
	if field_obj:
		lo, hi = field_obj.to_db(lo), field_obj.to_db(hi)
	return f"{col} BETWEEN ? AND ?", [lo, hi]


_LOOKUP_DISPATCH: dict[str, Any] = {
	"in": _compile_in,
	"not_in": _compile_not_in,
	"is_null": _compile_is_null,
	"between": _compile_between,
	"range": _compile_between,
}
