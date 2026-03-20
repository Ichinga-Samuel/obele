"""Fluent query builder for the ORM with sync and async APIs.

``QuerySet`` instances are returned from ``Model.filter()`` and friends.
They are lazily evaluated - SQL is only executed when results are
materialized (via ``all()``, ``first()``, iteration, etc.).

Async counterparts are prefixed with ``a`` (for example ``aall`` and
``afirst``).
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from typing import Any, Iterator, AsyncIterator, TYPE_CHECKING

from .database import Database
from .exceptions import RecordNotFoundError, MultipleResultsError
from .fields import ForeignKeyField

if TYPE_CHECKING:
    from .model import Model, ReverseRelationDescriptor

# ---------------------------------------------------------------------------
# Lookup registry
# ---------------------------------------------------------------------------

_LOOKUPS: dict[str, str] = {
    # Basic comparisons
    "exact": "= ?",
    "ne": "!= ?",
    "gt": "> ?",
    "gte": ">= ?",
    "lt": "< ?",
    "lte": "<= ?",
    # Pattern matching
    "like": "LIKE ?",
    "glob": "GLOB ?",
    # Special handling (value expansion required)
    "in": "IN",
    "not_in": "NOT_IN",
    "is_null": "IS",
    "between": "BETWEEN",
    "range": "BETWEEN",
    # String lookups (case-sensitive)
    "contains": "CONTAINS",
    "startswith": "STARTSWITH",
    "endswith": "ENDSWITH",
    # String lookups (case-insensitive)
    "iexact": "IEXACT",
    "icontains": "ICONTAINS",
    "istartswith": "ISTARTSWITH",
    "iendswith": "IENDSWITH",
    # Regex
    "regex": "REGEXP ?",
}

# Characters that need escaping in LIKE patterns
_LIKE_ESCAPE_CHAR = "\\"


def _escape_like(value: str) -> str:
    """Escape ``%``, ``_``, and ``\\`` for LIKE patterns."""
    return (
        value
        .replace(_LIKE_ESCAPE_CHAR, _LIKE_ESCAPE_CHAR * 2)
        .replace("%", _LIKE_ESCAPE_CHAR + "%")
        .replace("_", _LIKE_ESCAPE_CHAR + "_")
    )


def _parse_lookup(key: str) -> tuple[str, str]:
    """Split ``'field__lookup'`` into ``('field', 'lookup')``.

    If no double-underscore lookup is found the default ``'exact'`` is used.
    """
    parts = key.rsplit("__", 1)
    if len(parts) == 2 and parts[1] in _LOOKUPS:
        return parts[0], parts[1]
    return key, "exact"


# ===========================================================================
# Q - composable boolean expressions
# ===========================================================================

class Q:
    """Composable boolean query expression.

    Supports ``&`` (AND), ``|`` (OR), and ``~`` (NOT)::

        Q(name="Alice") | Q(name="Bob")
        ~Q(age__lt=18)
    """

    def __init__(self, *children: Q, **lookups: Any) -> None:
        self.children: list[Q | tuple[str, Any]] = [*children, *lookups.items()]
        self.connector: str = "AND"
        self.negated: bool = False

    def _combine(self, other: Q, connector: str) -> Q:
        combined = Q(self, other)
        combined.connector = connector
        return combined

    def __and__(self, other: Q) -> Q:
        return self._combine(other, "AND")

    def __or__(self, other: Q) -> Q:
        return self._combine(other, "OR")

    def __invert__(self) -> Q:
        clone = copy.deepcopy(self)
        clone.negated = not clone.negated
        return clone


# ===========================================================================
# Expression framework - F, Value, RawSQL, Func, aggregates, Subquery
# ===========================================================================

class Expression:
    """Base class for SQL expressions."""
    is_aggregate: bool = False

    def as_sql(self, queryset: QuerySet) -> tuple[str, list[Any]]:
        raise NotImplementedError


class Value(Expression):
    """Wrap a literal Python value as an SQL parameter."""
    def __init__(self, value: Any) -> None:
        self.value = value

    def as_sql(self, queryset: QuerySet) -> tuple[str, list[Any]]:
        del queryset
        return "?", [self.value]


class F(Expression):
    """Reference a model field (or joined field) by dotted path."""
    def __init__(self, field_path: str) -> None:
        self.field_path = field_path

    def as_sql(self, queryset: QuerySet) -> tuple[str, list[Any]]:
        column_sql, _ = queryset._resolve_field_reference(self.field_path.split("__"))
        return column_sql, []


class RawSQL(Expression):
    """Inject raw SQL with optional parameter bindings."""
    def __init__(self, sql: str, params: list[Any] | tuple[Any, ...] | None = None) -> None:
        self.sql = sql
        self.params = list(params or [])

    def as_sql(self, queryset: QuerySet) -> tuple[str, list[Any]]:
        del queryset
        return self.sql, list(self.params)


class Func(Expression):
    """Call an SQL function with arguments."""
    def __init__(self, name: str, *args: Any, is_aggregate: bool = False) -> None:
        self.name = name
        self.args = args
        self.is_aggregate = is_aggregate

    def as_sql(self, queryset: QuerySet) -> tuple[str, list[Any]]:
        sql_parts: list[str] = []
        params: list[Any] = []
        for arg in self.args:
            expr = queryset._coerce_expression(arg)
            arg_sql, arg_params = expr.as_sql(queryset)
            sql_parts.append(arg_sql)
            params.extend(arg_params)
        return f"{self.name}({', '.join(sql_parts)})", params


class Count(Func):
    def __init__(self, *args: Any) -> None:
        super().__init__("COUNT", *args, is_aggregate=True)


class Sum(Func):
    def __init__(self, *args: Any) -> None:
        super().__init__("SUM", *args, is_aggregate=True)


class Avg(Func):
    def __init__(self, *args: Any) -> None:
        super().__init__("AVG", *args, is_aggregate=True)


class Min(Func):
    def __init__(self, *args: Any) -> None:
        super().__init__("MIN", *args, is_aggregate=True)


class Max(Func):
    def __init__(self, *args: Any) -> None:
        super().__init__("MAX", *args, is_aggregate=True)


class Subquery(Expression):
    """Embed another QuerySet as a subquery."""
    def __init__(self, queryset: QuerySet, field: str | None = None) -> None:
        self.queryset = queryset
        self.field = field

    def as_sql(self, queryset: QuerySet) -> tuple[str, list[Any]]:
        del queryset
        field_name = self.field or self.queryset.model_cls._pk_name
        column_sql, _ = self.queryset._resolve_field_reference(field_name.split("__"))
        return self.queryset._build_select(select_override=[column_sql], include_annotations=False)


# ===========================================================================
# JoinSpec
# ===========================================================================

@dataclass
class _JoinSpec:
    path: tuple[str, ...]
    alias: str
    related_model: type[Model]
    relation_name: str
    relation_kind: str
    relation_field_name: str
    sql: str


# ===========================================================================
# QuerySet
# ===========================================================================

class QuerySet:
    """Lazy, chainable SQL query builder with sync and async APIs.

    Every mutating method returns a **copy** so the original can be reused.
    Async methods are prefixed with ``a`` (e.g. ``aall``, ``afirst``).
    """

    def __init__(self, model_cls: type[Model]) -> None:
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

    def __iter__(self) -> Iterator[Model]:
        return self.iterator()

    async def __aiter__(self) -> AsyncIterator[Model]:
        async for item in self.aiterator():
            yield item

    def __len__(self):
        return self.count()

    def __repr__(self) -> str:
        sql, params = self._build_select()
        return f"<QuerySet sql={sql!r} params={params!r}>"

    def _clone(self) -> QuerySet:
        return copy.deepcopy(self)

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def filter(self, *conditions: Q, **kwargs: Any) -> QuerySet:
        """Add ``WHERE`` conditions (AND logic).

        Accepts keyword lookups and/or ``Q`` objects::

            User.filter(name="Alice", age__gt=18)
            User.filter(Q(name="Alice") | Q(name="Bob"))
        """
        qs = self._clone()
        if kwargs:
            conditions = (*conditions, Q(**kwargs))
        for condition in conditions:
            if not isinstance(condition, Q):
                raise TypeError("filter() positional arguments must be Q objects")
            sql, params = qs._compile_q(condition)
            qs._where_fragments.append((sql, params))
        return qs

    def exclude(self, *conditions: Q, **kwargs: Any) -> QuerySet:
        """Add negated ``WHERE`` conditions."""
        qs = self._clone()
        if kwargs:
            conditions = (*conditions, Q(**kwargs))
        for condition in conditions:
            if not isinstance(condition, Q):
                raise TypeError("exclude() positional arguments must be Q objects")
            sql, params = qs._compile_q(~condition)
            qs._where_fragments.append((sql, params))
        return qs

    # ------------------------------------------------------------------
    # Ordering / pagination
    # ------------------------------------------------------------------

    def order_by(self, *fields: str) -> QuerySet:
        qs = self._clone()
        for field_name in fields:
            direction = "DESC" if field_name.startswith("-") else "ASC"
            raw_name = field_name[1:] if field_name.startswith("-") else field_name
            if raw_name in qs._annotations:
                qs._order_fields.append(f"{raw_name} {direction}")
                continue
            column_sql, _ = qs._resolve_field_reference(raw_name.split("__"))
            qs._order_fields.append(f"{column_sql} {direction}")
        return qs

    def limit(self, n: int) -> QuerySet:
        qs = self._clone()
        qs._limit_val = n
        return qs

    def offset(self, n: int) -> QuerySet:
        qs = self._clone()
        qs._offset_val = n
        return qs

    # ------------------------------------------------------------------
    # Joins
    # ------------------------------------------------------------------

    def join(self, relation_name: str, *, join_type: str = "INNER") -> QuerySet:
        """Explicitly join on a relation (forward FK or reverse)."""
        qs = self._clone()
        qs._ensure_join(tuple(relation_name.split("__")), join_type=join_type)
        return qs

    def select_related(self, *fk_fields: str) -> QuerySet:
        """Eagerly join on ForeignKeyField columns and hydrate related objects."""
        qs = self._clone()
        for fk_name in fk_fields:
            if "__" in fk_name:
                raise ValueError("select_related() currently supports only direct foreign keys")
            field_obj = qs.model_cls._fields.get(fk_name)
            if not isinstance(field_obj, ForeignKeyField):
                raise ValueError(f"'{fk_name}' is not a ForeignKeyField")

            join_spec = qs._ensure_join((fk_name,), join_type="LEFT")
            related_model = join_spec.related_model
            qs._selected_related[fk_name] = join_spec
            for field in related_model._fields.values():
                qs._select_fields.append(
                    f"{join_spec.alias}.{field.column_name} AS {fk_name}__{field.column_name}"
                )
        return qs

    # ------------------------------------------------------------------
    # Annotations
    # ------------------------------------------------------------------

    def annotate(self, **annotations: Any) -> QuerySet:
        """Add computed columns via expressions::

            User.annotate(post_count=Count(F("post_set__id")))
        """
        qs = self._clone()
        for alias, expression in annotations.items():
            qs._annotations[alias] = qs._coerce_expression(expression)
        return qs

    # ------------------------------------------------------------------
    # SQL building
    # ------------------------------------------------------------------

    def _build_select(
        self,
        *,
        select_override: list[str] | None = None,
        include_annotations: bool = True,
    ) -> tuple[str, list[Any]]:
        params: list[Any] = []
        select_parts = list(select_override or self._select_fields)

        if include_annotations and self._annotations:
            for alias, expression in self._annotations.items():
                expr_sql, expr_params = expression.as_sql(self)
                select_parts.append(f"{expr_sql} AS {alias}")
                params.extend(expr_params)

        parts = [
            "SELECT",
            ", ".join(select_parts),
            "FROM",
            self.model_cls.table_name,
        ]
        for join_spec in self._join_specs:
            parts.append(join_spec.sql)

        if self._where_fragments:
            parts.append("WHERE")
            parts.append(" AND ".join(fragment for fragment, _ in self._where_fragments))
            params.extend(self._where_params())

        if include_annotations and any(expr.is_aggregate for expr in self._annotations.values()):
            pk_col = self.model_cls._pk_field.column_name
            parts.append(f"GROUP BY {self.model_cls.table_name}.{pk_col}")

        if self._order_fields:
            parts.append("ORDER BY")
            parts.append(", ".join(self._order_fields))
        if self._limit_val is not None:
            parts.append(f"LIMIT {self._limit_val}")
        elif self._offset_val is not None:
            parts.append("LIMIT -1")
        if self._offset_val is not None:
            parts.append(f"OFFSET {self._offset_val}")
        return " ".join(parts), params

    def _where_params(self) -> list[Any]:
        params: list[Any] = []
        for _, fragment_params in self._where_fragments:
            params.extend(fragment_params)
        return params

    # ------------------------------------------------------------------
    # Row -> instance conversion (with hydration + annotations)
    # ------------------------------------------------------------------

    def _row_to_instance(self, row: Any) -> Model:
        """Build a model instance from a DB row, hydrating related objects."""
        row_dict = dict(row)

        # Extract base data
        base_data = {
            field.column_name: row_dict[field.column_name]
            for field in self.model_cls._fields.values()
            if field.column_name in row_dict
        }
        # Extract annotations
        annotations = {
            alias: row_dict[alias]
            for alias in self._annotations
            if alias in row_dict
        }

        instance = self.model_cls._from_row(base_data, annotations=annotations)

        # Hydrate related objects from select_related
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

    # ------------------------------------------------------------------
    # Materialisation
    # ------------------------------------------------------------------

    def all(self) -> list[Model]:
        """Execute the query and return a list of model instances."""
        return list(self.iterator())

    def first(self) -> Model | None:
        """Return the first result or ``None``."""
        for instance in self.limit(1).iterator():
            return instance
        return None

    def get(self, **kwargs: Any) -> Model:
        """Return exactly one result.  Raises on 0 or >1 matches."""
        qs = self.filter(**kwargs) if kwargs else self._clone()
        results = list(qs.limit(2).iterator())
        if len(results) == 0:
            raise RecordNotFoundError(
                f"No {self.model_cls.__name__} matches the given query"
            )
        if len(results) > 1:
            raise MultipleResultsError(
                f"Expected 1 {self.model_cls.__name__}, got {len(results)}"
            )
        return results[0]

    # ------------------------------------------------------------------
    # Streaming iteration
    # ------------------------------------------------------------------

    def iterator(self, chunk_size: int = 2000) -> Iterator[Model]:
        """Stream results row-by-row without materializing the full list.

        Uses ``cursor.fetchmany(chunk_size)`` to read in chunks.
        """
        sql, params = self._build_select()
        cursor = Database.execute_read(sql, params)
        while True:
            rows = cursor.fetchmany(chunk_size)
            if not rows:
                break
            for row in rows:
                yield self._row_to_instance(row)

    async def aiterator(self, chunk_size: int = 2000) -> AsyncIterator[Model]:
        """Async streaming results.

        Fetches chunks via ``asyncio.to_thread`` and yields instances.
        """
        sql, params = self._build_select()
        cursor = await asyncio.to_thread(Database.execute_read, sql, params)
        while True:
            rows = await asyncio.to_thread(cursor.fetchmany, chunk_size)
            if not rows:
                break
            for row in rows:
                yield self._row_to_instance(row)

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def count(self) -> int:
        """Return the count of matching rows."""
        sql, params = self._build_select()
        count_sql = f"SELECT COUNT(*) AS cnt FROM ({sql})"
        return int(Database.fetch_value(count_sql, params, column="cnt") or 0)

    def exists(self) -> bool:
        """Return ``True`` if at least one row matches."""
        return self.count() > 0

    def aggregate(self, func: str, field: str) -> Any:
        """Run an aggregate function (SUM, AVG, MIN, MAX, COUNT)."""
        func = func.upper()
        if func not in ("SUM", "AVG", "MIN", "MAX", "COUNT"):
            raise ValueError(f"Unsupported aggregate function: {func}")
        col, _ = self._resolve_field_reference(field.split("__"))
        base_sql, params = self._build_select(
            select_override=[f"{col} AS __agg_target__"],
            include_annotations=False,
        )
        agg_sql = f"SELECT {func}(__agg_target__) AS result FROM ({base_sql})"
        return Database.fetch_value(agg_sql, params, column="result")

    # ------------------------------------------------------------------
    # Bulk write operations
    # ------------------------------------------------------------------

    def update(self, *, validate: bool = True, **kwargs: Any) -> int:
        """Bulk UPDATE matching rows.  Returns number of rows affected.

        When *validate* is ``True`` (default), each value is validated
        against its field constraints before the SQL is executed.
        """
        set_parts: list[str] = []
        set_params: list[Any] = []
        for key, value in kwargs.items():
            field_obj = self.model_cls._fields.get(key)
            col = field_obj.column_name if field_obj else key
            if validate and field_obj:
                field_obj.validate(value)
            db_val = field_obj.to_db(value) if field_obj else value
            set_parts.append(f"{col} = ?")
            set_params.append(db_val)

        sql = f"UPDATE {self.model_cls.table_name} SET {', '.join(set_parts)}"
        if self._where_fragments:
            sql += " WHERE " + " AND ".join(fragment for fragment, _ in self._where_fragments)
        all_params = set_params + self._where_params()
        cursor = Database.execute(sql, all_params)
        return cursor.rowcount

    def delete(self) -> int:
        """Bulk DELETE matching rows.  Returns number of rows affected."""
        sql = f"DELETE FROM {self.model_cls.table_name}"
        if self._where_fragments:
            sql += " WHERE " + " AND ".join(fragment for fragment, _ in self._where_fragments)
        cursor = Database.execute(sql, self._where_params())
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Async variants
    # ------------------------------------------------------------------

    async def aall(self) -> list[Model]:
        return await asyncio.to_thread(self.all)

    async def afirst(self) -> Model | None:
        return await asyncio.to_thread(self.first)

    async def aget(self, **kwargs: Any) -> Model:
        return await asyncio.to_thread(self.get, **kwargs)

    async def acount(self) -> int:
        return await asyncio.to_thread(self.count)

    async def aexists(self) -> bool:
        return await asyncio.to_thread(self.exists)

    async def aaggregate(self, func: str, field: str) -> Any:
        return await asyncio.to_thread(self.aggregate, func, field)

    async def aupdate(self, **kwargs: Any) -> int:
        return await asyncio.to_thread(self.update, **kwargs)

    async def adelete(self) -> int:
        return await asyncio.to_thread(self.delete)

    # ------------------------------------------------------------------
    # Internal: Q compilation
    # ------------------------------------------------------------------

    def _split_lookup(self, key: str) -> tuple[list[str], str]:
        """Split a key into field path parts and lookup name."""
        parts = key.split("__")
        if parts[-1] in _LOOKUPS:
            return parts[:-1], parts[-1]
        return parts, "exact"

    def _compile_q(self, expression: Q) -> tuple[str, list[Any]]:
        compiled_children: list[str] = []
        params: list[Any] = []
        for child in expression.children:
            if isinstance(child, Q):
                child_sql, child_params = self._compile_q(child)
            else:
                child_sql, child_params = self._compile_condition(*child)
            compiled_children.append(f"({child_sql})")
            params.extend(child_params)

        if not compiled_children:
            compiled_sql = "1=1"
        else:
            compiled_sql = f" {expression.connector} ".join(compiled_children)
        if expression.negated:
            compiled_sql = f"NOT ({compiled_sql})"
        return compiled_sql, params

    def _compile_condition(self, key: str, value: Any) -> tuple[str, list[Any]]:
        """Compile a single lookup condition into SQL and params."""
        field_parts, lookup = self._split_lookup(key)
        column_sql, field_obj = self._resolve_field_reference(field_parts)

        # Special-case lookups
        if lookup == "in":
            if isinstance(value, QuerySet):
                value = Subquery(value)
            if isinstance(value, Subquery):
                sub_sql, sub_params = value.as_sql(self)
                return f"{column_sql} IN ({sub_sql})", sub_params
            if not isinstance(value, (list, tuple, set)):
                value = [value]
            values = list(value)
            placeholders = ", ".join("?" for _ in values)
            params = [field_obj.to_db(v) if field_obj else v for v in values]
            return f"{column_sql} IN ({placeholders})", params

        if lookup == "not_in":
            if not isinstance(value, (list, tuple, set)):
                value = [value]
            values = list(value)
            placeholders = ", ".join("?" for _ in values)
            params = [field_obj.to_db(v) if field_obj else v for v in values]
            return f"{column_sql} NOT IN ({placeholders})", params

        if lookup == "is_null":
            return (
                f"{column_sql} {'IS NULL' if value else 'IS NOT NULL'}",
                [],
            )

        if lookup in ("between", "range"):
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise ValueError(
                    f"'{lookup}' lookup requires a 2-element tuple/list, "
                    f"got {type(value).__name__}"
                )
            lo, hi = value
            if field_obj:
                lo = field_obj.to_db(lo)
                hi = field_obj.to_db(hi)
            return f"{column_sql} BETWEEN ? AND ?", [lo, hi]

        if lookup == "contains":
            escaped = _escape_like(str(value))
            return f"{column_sql} LIKE ? ESCAPE '\\'", [f"%{escaped}%"]

        if lookup == "startswith":
            escaped = _escape_like(str(value))
            return f"{column_sql} LIKE ? ESCAPE '\\'", [f"{escaped}%"]

        if lookup == "endswith":
            escaped = _escape_like(str(value))
            return f"{column_sql} LIKE ? ESCAPE '\\'", [f"%{escaped}"]

        if lookup == "iexact":
            return f"{column_sql} LIKE ? ESCAPE '\\'", [str(value)]

        if lookup == "icontains":
            escaped = _escape_like(str(value))
            return f"{column_sql} LIKE ? ESCAPE '\\'", [f"%{escaped}%"]

        if lookup == "istartswith":
            escaped = _escape_like(str(value))
            return f"{column_sql} LIKE ? ESCAPE '\\'", [f"{escaped}%"]

        if lookup == "iendswith":
            escaped = _escape_like(str(value))
            return f"{column_sql} LIKE ? ESCAPE '\\'", [f"%{escaped}"]

        # Expression-based values (Subquery, F, etc.)
        if isinstance(value, QuerySet):
            value = Subquery(value)

        if isinstance(value, Subquery):
            sub_sql, sub_params = value.as_sql(self)
            operator = _LOOKUPS[lookup].replace("?", f"({sub_sql})")
            return f"{column_sql} {operator}", sub_params

        if isinstance(value, Expression):
            expr_sql, expr_params = value.as_sql(self)
            operator = _LOOKUPS[lookup].replace("?", expr_sql)
            return f"{column_sql} {operator}", expr_params

        # Standard single-value lookups
        db_val = field_obj.to_db(value) if field_obj else value
        return f"{column_sql} {_LOOKUPS[lookup]}", [db_val]

    # ------------------------------------------------------------------
    # Internal: expression coercion
    # ------------------------------------------------------------------

    def _coerce_expression(self, value: Any) -> Expression:
        if isinstance(value, Expression):
            return value
        if isinstance(value, QuerySet):
            return Subquery(value)
        if isinstance(value, str) and "__" in value:
            return F(value)
        return Value(value)

    # ------------------------------------------------------------------
    # Internal: field reference resolution & join management
    # ------------------------------------------------------------------

    def _resolve_field_reference(self, path_parts: list[str]) -> tuple[str, Any]:
        """Resolve a dotted field path to ``(table.column_sql, field_obj)``."""
        if not path_parts:
            raise ValueError("field path cannot be empty")

        current_model = self.model_cls
        current_alias = self.model_cls.table_name

        for idx, segment in enumerate(path_parts[:-1], start=1):
            join_spec = self._ensure_join(tuple(path_parts[:idx]), join_type="LEFT")
            current_model = join_spec.related_model
            current_alias = join_spec.alias

        field_name = path_parts[-1]
        field_obj = current_model._fields.get(field_name)
        if field_obj is None:
            raise ValueError(
                f"Unknown field path '{'__'.join(path_parts)}' for {self.model_cls.__name__}"
            )
        return f"{current_alias}.{field_obj.column_name}", field_obj

    def _ensure_join(self, path: tuple[str, ...], *, join_type: str = "LEFT") -> _JoinSpec:
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

        # Forward FK join
        if relation_name in parent_model._fields:
            field_obj = parent_model._fields[relation_name]
            if not isinstance(field_obj, ForeignKeyField):
                raise ValueError(f"'{relation_name}' is not a joinable ForeignKeyField")
            related_model = field_obj.related_model
            related_pk = related_model._pk_field.column_name
            sql = (
                f"{join_type} JOIN {related_model.table_name} AS {alias} "
                f"ON {parent_alias}.{field_obj.column_name} = {alias}.{related_pk}"
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
            # Reverse relation join
            reverse_relations: dict[str, ReverseRelationDescriptor] = getattr(
                parent_model,
                "_reverse_relations",
                {},
            )
            descriptor = reverse_relations.get(relation_name)
            if descriptor is None:
                raise ValueError(
                    f"'{relation_name}' is not a joinable relation on {parent_model.__name__}"
                )
            related_model = descriptor.related_model
            fk_field = related_model._fields[descriptor.field_name]
            parent_pk = parent_model._pk_field.column_name
            sql = (
                f"{join_type} JOIN {related_model.table_name} AS {alias} "
                f"ON {alias}.{fk_field.column_name} = {parent_alias}.{parent_pk}"
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
