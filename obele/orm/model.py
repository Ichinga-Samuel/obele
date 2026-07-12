"""obele model base class and metaclass with sync and async APIs.

``Model`` maps Python classes to SQLite tables with full CRUD support,
schema management, and query-builder integration.  Async methods are
prefixed with ``a`` and run the sync implementation on a worker thread,
so transactions and scoped bindings behave identically in both worlds::

    users = User.filter(age__gt=18).order_by("-name").limit(10).all()
    users = await User.filter(age__gt=18).order_by("-name").limit(10).aall()
"""

from __future__ import annotations

import functools
from collections.abc import Iterable, Sequence
from typing import Any, Callable, ClassVar

from .database import Database, athread, awrite
from .fields import Field, IntegerField, ForeignKeyField, _MISSING
from .query import QuerySet
from .exceptions import RecordNotFoundError, FieldValidationError, MigrationError
from .signals import pre_save, post_save, pre_delete, post_delete, pre_create, post_create
from .sql import validate_identifier

# Global registry so ForeignKeyField can resolve string references lazily.
_model_registry: dict[str, type[Model]] = {}

# QuerySet methods reachable directly on the model class (``User.filter(...)``).
_QUERYSET_PROXY = frozenset({
    "filter", "exclude", "order_by", "limit", "offset", "distinct",
    "values", "values_list", "only", "defer", "join", "select_related",
    "prefetch_related", "annotate", "group_by", "having", "iterator", "aiterator",
    "all", "aall", "first", "afirst", "last", "alast", "get", "aget",
    "count", "acount", "exists", "aexists", "aggregate", "aaggregate",
    "in_bulk", "ain_bulk", "latest", "earliest", "alatest", "aearliest",
    "paginate", "apaginate", "cursor_paginate", "acursor_paginate",
    "as_sql", "explain",
})


class ReverseRelationManager:
    """Query helper for reverse foreign-key access.

    Supports the full QuerySet API plus ``create()``.  When the owning
    instance was loaded with ``prefetch_related``, ``all()`` and iteration
    serve the cached rows without extra queries::

        user.posts.all()
        user.posts.filter(published=True).count()
        user.posts.create(title="New Post")
    """

    __slots__ = ("instance", "related_model", "field_name", "accessor_name")

    def __init__(self, instance: Model, related_model: type[Model], field_name: str, accessor_name: str) -> None:
        self.instance = instance
        self.related_model = related_model
        self.field_name = field_name  # FK attribute on the related model
        self.accessor_name = accessor_name

    def __repr__(self) -> str:
        return (
            f"<ReverseRelationManager owner={type(self.instance).__name__} "
            f"related={self.related_model.__name__} field={self.field_name!r} pk={self.instance.pk}>"
        )

    @property
    def _prefetched(self) -> list[Model] | None:
        return self.instance.__dict__.get(f"_prefetch_{self.accessor_name}")

    def _queryset(self) -> QuerySet:
        pk = self.instance.pk
        if pk is None:
            raise RecordNotFoundError("Cannot use a reverse relation on an unsaved instance")
        return self.related_model.filter(**{self.field_name: pk})

    def __getattr__(self, name: str) -> Any:
        return getattr(self._queryset(), name)

    def all(self) -> list[Model]:
        cached = self._prefetched
        return list(cached) if cached is not None else self._queryset().all()

    async def aall(self) -> list[Model]:
        cached = self._prefetched
        return list(cached) if cached is not None else await self._queryset().aall()

    def count(self) -> int:
        cached = self._prefetched
        return len(cached) if cached is not None else self._queryset().count()

    async def acount(self) -> int:
        cached = self._prefetched
        return len(cached) if cached is not None else await self._queryset().acount()

    def create(self, **kwargs: Any) -> Model:
        """Create a related object with the FK pre-set to this instance."""
        kwargs[self.field_name] = self.instance
        return self.related_model.create(**kwargs)

    async def acreate(self, **kwargs: Any) -> Model:
        """Async version of :meth:`create`."""
        kwargs[self.field_name] = self.instance
        return await self.related_model.acreate(**kwargs)

    def __iter__(self):
        cached = self._prefetched
        return iter(cached) if cached is not None else iter(self._queryset())

    async def __aiter__(self):
        cached = self._prefetched
        if cached is not None:
            for item in cached:
                yield item
            return
        async for item in self._queryset():
            yield item


class ReverseRelationDescriptor:
    """Installed on the target Model; returns a bound :class:`ReverseRelationManager`."""

    __slots__ = ("related_model", "field_name", "accessor_name")

    def __init__(self, related_model: type[Model], field_name: str, accessor_name: str) -> None:
        self.related_model = related_model
        self.field_name = field_name
        self.accessor_name = accessor_name

    def __get__(self, instance: Model | None, owner: type[Model]) -> Any:
        if instance is None:
            return self
        return ReverseRelationManager(instance, self.related_model, self.field_name, self.accessor_name)


def _register_reverse_relations() -> None:
    """Scan the registry and install reverse relation descriptors."""
    for source_model in _model_registry.values():
        for field_name, field in source_model._fields.items():
            if not isinstance(field, ForeignKeyField):
                continue
            try:
                related_model = field.related_model
            except Exception:
                continue  # string reference not registered yet

            accessor_name = field.related_name or f"{source_model.__name__.lower()}_set"
            reverse_relations = related_model.__dict__.get("_reverse_relations", {})
            if accessor_name in reverse_relations:
                continue

            existing = getattr(related_model, accessor_name, None)
            if existing is not None and not isinstance(existing, ReverseRelationDescriptor):
                raise ValueError(
                    f"Cannot register reverse relation '{accessor_name}' on "
                    f"{related_model.__name__}: attribute already exists"
                )

            descriptor = ReverseRelationDescriptor(source_model, field_name, accessor_name)
            setattr(related_model, accessor_name, descriptor)
            reverse_relations = dict(reverse_relations)
            reverse_relations[accessor_name] = descriptor
            related_model._reverse_relations = reverse_relations


class MetaModel(type):
    """Collect ``Field`` descriptors and precompute per-model SQL plans."""

    def __new__(mcs, cls_name: str, bases: tuple[type, ...], namespace: dict[str, Any]) -> type:
        fields: dict[str, Field] = {}
        for base in bases:
            if hasattr(base, "_fields"):
                fields.update(base._fields)
            else:  # mixin bases contributing Field attributes
                for attr_name in dir(base):
                    if isinstance(attr_val := getattr(base, attr_name, None), Field):
                        fields[attr_name] = attr_val
        for attr_name, attr_val in namespace.items():
            if isinstance(attr_val, Field):
                fields[attr_name] = attr_val

        namespace["_fields"] = fields
        namespace.setdefault("_reverse_relations", {})
        if not namespace.get("table_name"):
            namespace["table_name"] = cls_name.lower()
        validate_identifier(namespace["table_name"], kind="table name")

        cls = super().__new__(mcs, cls_name, bases, namespace)
        is_concrete = any(hasattr(b, "_fields") for b in bases)

        pk_field = next((f for f in fields.values() if f.primary_key), None)
        if pk_field is None and is_concrete:
            pk_field = IntegerField(primary_key=True)
            pk_field.__set_name__(cls, "id")
            fields["id"] = pk_field
            setattr(cls, "id", pk_field)
        cls._pk_field = pk_field
        cls._pk_name = pk_field.attr_name if pk_field else "id"
        # _pk_field is a live descriptor; instance code must use these
        # plain-string ClassVars (or ``type(self)._pk_field``) instead.
        cls._pk_col = pk_field.column_name if pk_field else "id"
        cls._pk_is_auto = isinstance(pk_field, IntegerField)

        if is_concrete:
            mcs._build_plans(cls)
            cls._unique_together = getattr(cls, "unique_together", [])
            cls._index_together = getattr(cls, "index_together", [])
            cls._check_constraints = getattr(cls, "check_constraints", [])

        if cls_name != "Model":
            _model_registry[cls_name] = cls
            _register_reverse_relations()
        return cls

    def __getattr__(cls, name: str) -> Any:
        # Expose the QuerySet API directly on the model class.
        if name in _QUERYSET_PROXY:
            return getattr(cls._queryset(), name)
        raise AttributeError(f"type object {cls.__name__!r} has no attribute {name!r}")

    @staticmethod
    def _build_plans(cls: type) -> None:
        """Precompute hydration and INSERT plans for this model."""
        fields: dict[str, Field] = cls._fields
        cls._hydration = tuple(
            (name, f.column_name, f.to_python, f.cache_attr_name if isinstance(f, ForeignKeyField) else None)
            for name, f in fields.items()
        )
        cls._non_pk_field_names = tuple(n for n, f in fields.items() if not f.primary_key)
        cls._all_field_names = tuple(fields)
        cls._returning_cols = ", ".join(f.column_name for f in fields.values())

        def insert_sql(names: Sequence[str]) -> str:
            cols = ", ".join(fields[n].column_name for n in names)
            marks = ", ".join("?" for _ in names)
            return f"INSERT INTO {cls.table_name} ({cols}) VALUES ({marks})"

        cls._insert_sql = insert_sql(cls._non_pk_field_names) if cls._non_pk_field_names else \
            f"INSERT INTO {cls.table_name} DEFAULT VALUES"
        cls._insert_sql_with_pk = insert_sql(cls._all_field_names)


class Model(metaclass=MetaModel):
    """Base class for all ORM models.

    Subclass and declare ``Field`` class attributes to define the schema::

        class User(Model):
            table_name = "users"
            name = TextField()
            age  = IntegerField(nullable=True)

        User.create_table()
        alice = User.create(name="Alice", age=30)
        alice.name = "Alicia"
        alice.save()

    The full QuerySet API is available on the class itself
    (``User.filter(...)``, ``User.order_by(...)``, ``await User.aall()``).
    """

    table_name: ClassVar[str] = ""
    unique_together: ClassVar[list[tuple[str, ...]]] = []
    index_together: ClassVar[list[tuple[str, ...]]] = []
    check_constraints: ClassVar[list[str]] = []
    _fields: ClassVar[dict[str, Field]]
    _pk_field: ClassVar[Field | None]
    _pk_name: ClassVar[str]
    _pk_col: ClassVar[str]
    _pk_is_auto: ClassVar[bool]
    _reverse_relations: ClassVar[dict[str, ReverseRelationDescriptor]]
    _hydration: ClassVar[tuple[tuple[str, str, Callable[[Any], Any], str | None], ...]]
    _non_pk_field_names: ClassVar[tuple[str, ...]]
    _all_field_names: ClassVar[tuple[str, ...]]
    _returning_cols: ClassVar[str]
    _insert_sql: ClassVar[str]
    _insert_sql_with_pk: ClassVar[str]
    _unique_together: ClassVar[list[tuple[str, ...]]]
    _index_together: ClassVar[list[tuple[str, ...]]]
    _check_constraints: ClassVar[list[str]]

    def __init__(self, **kwargs: Any) -> None:
        for name, field in self._fields.items():
            if name in kwargs:
                setattr(self, name, kwargs[name])  # descriptor validates/coerces
            elif field.default is not _MISSING:
                setattr(self, name, field.default() if callable(field.default) else field.default)
            elif field.primary_key:
                self.__dict__[name] = None
        self._persisted = kwargs.get("_persisted", False)
        self._annotations: dict[str, Any] = kwargs.get("_annotations", {})
        self._snapshot: dict[str, Any] = {}
        if self._persisted:
            self._take_snapshot()

    # ------------------------------------------------------------------
    # Identity / state
    # ------------------------------------------------------------------

    @property
    def pk(self) -> Any:
        """The primary-key value, whatever the PK field is named."""
        return self.__dict__.get(self._pk_name)

    @pk.setter
    def pk(self, value: Any) -> None:
        setattr(self, self._pk_name, value)

    def _take_snapshot(self) -> None:
        self._snapshot = {name: self.__dict__.get(name) for name in self._non_pk_field_names}

    @property
    def dirty_fields(self) -> dict[str, Any]:
        """Fields changed since the instance was loaded or last saved."""
        if not self._persisted:
            return {n: self.__dict__.get(n) for n in self._non_pk_field_names}
        snapshot = self._snapshot
        return {
            name: value
            for name in self._non_pk_field_names
            if (value := self.__dict__.get(name)) != snapshot.get(name)
        }

    @property
    def is_dirty(self) -> bool:
        return bool(self.dirty_fields)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} pk={self.pk}>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, type(self)):
            return NotImplemented
        if self.pk is None or other.pk is None:
            return self is other
        return self.pk == other.pk

    def __hash__(self) -> int:
        return hash((type(self).__name__, self.pk))

    # ------------------------------------------------------------------
    # Schema DDL
    # ------------------------------------------------------------------

    @classmethod
    def _create_table_sql(cls, if_not_exists: bool = True) -> str:
        parts = [field.column_ddl() for field in cls._fields.values()]
        for group in cls._unique_together:
            cols = ", ".join(cls._fields[f].column_name for f in group if f in cls._fields)
            if cols:
                parts.append(f"UNIQUE ({cols})")
        parts.extend(f"CHECK ({expr})" for expr in cls._check_constraints)
        check = "IF NOT EXISTS " if if_not_exists else ""
        return f"CREATE TABLE {check}{cls.table_name} ({', '.join(parts)})"

    @classmethod
    def _create_index_sqls(cls) -> list[str]:
        indexes = [
            f"CREATE INDEX IF NOT EXISTS idx_{cls.table_name}_{f.column_name} "
            f"ON {cls.table_name} ({f.column_name})"
            for f in cls._fields.values()
            if f.index and not f.primary_key
        ]
        for group in cls._index_together:
            cols = [cls._fields[f].column_name for f in group if f in cls._fields]
            if cols:
                indexes.append(
                    f"CREATE INDEX IF NOT EXISTS idx_{cls.table_name}_{'_'.join(cols)} "
                    f"ON {cls.table_name} ({', '.join(cols)})"
                )
        return indexes

    @classmethod
    def create_table(cls, if_not_exists: bool = True) -> None:
        """Create the SQLite table (and declared indexes) for this model."""
        Database.execute(cls._create_table_sql(if_not_exists))
        for sql in cls._create_index_sqls():
            Database.execute(sql)

    @classmethod
    async def acreate_table(cls, if_not_exists: bool = True) -> None:
        """Async version of :meth:`create_table`."""
        await awrite(cls.create_table, if_not_exists)

    @classmethod
    def drop_table(cls, if_exists: bool = True) -> None:
        """Drop the SQLite table for this model."""
        check = "IF EXISTS " if if_exists else ""
        Database.execute(f"DROP TABLE {check}{cls.table_name}")

    @classmethod
    async def adrop_table(cls, if_exists: bool = True) -> None:
        """Async version of :meth:`drop_table`."""
        await awrite(cls.drop_table, if_exists)

    # ------------------------------------------------------------------
    # Migration
    # ------------------------------------------------------------------

    @classmethod
    def _table_exists(cls) -> bool:
        return Database.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            [cls.table_name],
        ) is not None

    @classmethod
    def _migration_default_value(cls, field: Field) -> Any:
        if field.db_default is not None or field.primary_key:
            return _MISSING
        if field.default is not _MISSING:
            value = field.default() if callable(field.default) else field.default
            field.validate(value)
            return field.to_db(value)
        if field.nullable:
            return _MISSING
        raise MigrationError(
            f"Cannot migrate {cls.__name__}: new non-nullable column "
            f"'{field.column_name}' has no default"
        )

    @classmethod
    def migrate(cls, *, rename_fields: dict[str, str] | None = None, create_if_missing: bool = True) -> None:
        """Schema-sync by rebuilding the table from the model definition.

        Handles column additions, removals, and renames in one transaction.

        Args:
            rename_fields: Map of ``{new_field_name: old_column_name}``.
            create_if_missing: Create the table when it doesn't exist yet.
        """
        if not cls._table_exists():
            if create_if_missing:
                cls.create_table()
                return
            raise MigrationError(f"Table '{cls.table_name}' does not exist")

        rename_fields = rename_fields or {}
        existing_columns = {row["name"] for row in Database.fetchall(f"PRAGMA table_info({cls.table_name})")}
        temp_table = f"{cls.table_name}__old"

        insert_columns: list[str] = []
        select_expressions: list[str] = []
        params: list[Any] = []
        for name, field in cls._fields.items():
            old_column = rename_fields.get(name, rename_fields.get(field.column_name, field.column_name))
            if old_column in existing_columns:
                insert_columns.append(field.column_name)
                select_expressions.append(old_column)
                continue
            default_value = cls._migration_default_value(field)
            if default_value is _MISSING:
                continue
            insert_columns.append(field.column_name)
            select_expressions.append("?")
            params.append(default_value)

        with Database.transaction() as conn:
            conn.execute(f"DROP TABLE IF EXISTS {temp_table}")
            conn.execute(f"ALTER TABLE {cls.table_name} RENAME TO {temp_table}")
            conn.execute(cls._create_table_sql(if_not_exists=False))
            if insert_columns:
                conn.execute(
                    f"INSERT INTO {cls.table_name} ({', '.join(insert_columns)}) "
                    f"SELECT {', '.join(select_expressions)} FROM {temp_table}",
                    params,
                )
            conn.execute(f"DROP TABLE {temp_table}")
            for sql in cls._create_index_sqls():
                conn.execute(sql)

    @classmethod
    async def amigrate(cls, **kwargs: Any) -> None:
        """Async version of :meth:`migrate`."""
        await awrite(functools.partial(cls.migrate, **kwargs))

    # ------------------------------------------------------------------
    # Instance persistence
    # ------------------------------------------------------------------

    def _validate_all(self) -> None:
        for name, field in self._fields.items():
            field.validate(self.__dict__.get(name))

    def save(self) -> None:
        """Insert or update this instance.

        Uses dirty tracking - only changed fields are sent on UPDATE.
        Emits ``pre_save`` / ``post_save`` (and ``*_create``) signals.
        """
        self._validate_all()
        created = not self._persisted or self.pk is None
        pre_save.send(type(self), instance=self, created=created)
        if created:
            pre_create.send(type(self), instance=self)
        if self._persisted and self.pk is not None:
            self._update()
        else:
            self._insert()
        self._take_snapshot()
        post_save.send(type(self), instance=self, created=created)
        if created:
            post_create.send(type(self), instance=self)

    async def asave(self) -> None:
        """Async version of :meth:`save`."""
        await awrite(self.save)

    def _insert(self) -> None:
        d = self.__dict__
        fields = self._fields
        auto_pk = self._pk_is_auto and d.get(self._pk_name) is None
        names = self._non_pk_field_names if auto_pk else self._all_field_names
        if names:
            sql = self._insert_sql if auto_pk else self._insert_sql_with_pk
            values = [fields[n].to_db(d.get(n)) for n in names]
            cursor = Database.execute(sql, values)
        else:
            cursor = Database.execute(f"INSERT INTO {self.table_name} DEFAULT VALUES")
        if d.get(self._pk_name) is None:
            d[self._pk_name] = cursor.lastrowid
        self._persisted = True

    def _update(self) -> None:
        dirty = self.dirty_fields
        if not dirty:
            return
        fields = self._fields
        set_parts = [f"{fields[name].column_name} = ?" for name in dirty]
        values = [fields[name].to_db(value) for name, value in dirty.items()]
        values.append(self.pk)
        Database.execute(
            f"UPDATE {self.table_name} SET {', '.join(set_parts)} WHERE {self._pk_col} = ?",
            values,
        )

    def delete(self) -> None:
        """Delete this instance.  Emits ``pre_delete`` / ``post_delete``."""
        if self.pk is None:
            raise RecordNotFoundError("Cannot delete an unsaved instance")
        pre_delete.send(type(self), instance=self)
        Database.execute(
            f"DELETE FROM {self.table_name} WHERE {self._pk_col} = ?",
            [self.pk],
        )
        self.__dict__[self._pk_name] = None
        self._persisted = False
        post_delete.send(type(self), instance=self)

    async def adelete(self) -> None:
        """Async version of :meth:`delete`."""
        await awrite(self.delete)

    def refresh(self) -> None:
        """Re-read this instance's data from the database."""
        if self.pk is None:
            raise RecordNotFoundError("Cannot refresh an unsaved instance")
        row = Database.fetchone(
            f"SELECT * FROM {self.table_name} WHERE {self._pk_col} = ?",
            [self.pk],
        )
        if row is None:
            raise RecordNotFoundError(f"{type(self).__name__} with pk={self.pk} not found")
        row_dict = dict(row)
        d = self.__dict__
        for attr, col, to_python, fk_cache in self._hydration:
            if col in row_dict:
                raw = row_dict[col]
                d[attr] = to_python(raw) if raw is not None else None
                if fk_cache is not None:
                    d.pop(fk_cache, None)
        self._take_snapshot()

    async def arefresh(self) -> None:
        """Async version of :meth:`refresh`."""
        await athread(self.refresh)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self, *, mode: str = "python", include_annotations: bool = True) -> dict[str, Any]:
        """Serialize all fields to a dict (``mode="python"`` or ``"db"``)."""
        if mode == "python":
            data = {name: self.__dict__.get(name) for name in self._fields}
        elif mode == "db":
            data = {name: field.to_db(self.__dict__.get(name)) for name, field in self._fields.items()}
        else:
            raise ValueError("mode must be 'python' or 'db'")
        if include_annotations:
            data.update(self._annotations)
        return data

    def to_db_dict(self, *, include_annotations: bool = True) -> dict[str, Any]:
        """Shorthand for ``to_dict(mode="db")``."""
        return self.to_dict(mode="db", include_annotations=include_annotations)

    @classmethod
    def _from_row(cls, row_dict: dict[str, Any], *, annotations: dict[str, Any] | None = None) -> Model:
        """Construct a persisted instance from a database row dict."""
        instance = cls.__new__(cls)
        d = instance.__dict__
        for attr, col, to_python, _ in cls._hydration:
            raw = row_dict.get(col)
            d[attr] = to_python(raw) if raw is not None else None
        d["_persisted"] = True
        ann = annotations or {}
        d["_annotations"] = ann
        d["_snapshot"] = {name: d.get(name) for name in cls._non_pk_field_names}
        for alias, value in ann.items():
            d[alias] = value
        return instance

    # ------------------------------------------------------------------
    # Creation helpers
    # ------------------------------------------------------------------

    @classmethod
    def create(cls, **kwargs: Any) -> Model:
        """Create, save, and return a new instance."""
        instance = cls(**kwargs)
        instance.save()
        return instance

    @classmethod
    async def acreate(cls, **kwargs: Any) -> Model:
        """Async version of :meth:`create`."""
        return await awrite(functools.partial(cls.create, **kwargs))

    @classmethod
    def get_or_create(cls, defaults: dict[str, Any] | None = None, **kwargs: Any) -> tuple[Model, bool]:
        """Return ``(instance, created)`` - fetch if it exists, else create."""
        with Database.transaction():
            try:
                return cls.get(**kwargs), False
            except RecordNotFoundError:
                return cls.create(**{**kwargs, **(defaults or {})}), True

    @classmethod
    async def aget_or_create(cls, defaults: dict[str, Any] | None = None, **kwargs: Any) -> tuple[Model, bool]:
        """Async version of :meth:`get_or_create`."""
        return await awrite(functools.partial(cls.get_or_create, defaults, **kwargs))

    @classmethod
    def update_or_create(cls, defaults: dict[str, Any] | None = None, **kwargs: Any) -> tuple[Model, bool]:
        """Fetch and update if it exists, otherwise create.  Returns ``(instance, created)``."""
        defaults = defaults or {}
        with Database.transaction():
            try:
                instance = cls.get(**kwargs)
            except RecordNotFoundError:
                return cls.create(**{**kwargs, **defaults}), True
            for key, val in defaults.items():
                setattr(instance, key, val)
            instance.save()
            return instance, False

    @classmethod
    async def aupdate_or_create(cls, defaults: dict[str, Any] | None = None, **kwargs: Any) -> tuple[Model, bool]:
        """Async version of :meth:`update_or_create`."""
        return await awrite(functools.partial(cls.update_or_create, defaults, **kwargs))

    @classmethod
    def get_or_none(cls, **kwargs: Any) -> Model | None:
        """Return one matching instance, or ``None`` when no row matches."""
        try:
            return cls.get(**kwargs)
        except RecordNotFoundError:
            return None

    @classmethod
    async def aget_or_none(cls, **kwargs: Any) -> Model | None:
        """Async version of :meth:`get_or_none`."""
        return await athread(functools.partial(cls.get_or_none, **kwargs))

    @classmethod
    def get_by_pk(cls, pk: Any) -> Model:
        """Fetch one instance by primary key."""
        if cls._queryset.__func__ is Model._queryset.__func__:
            # Fast path: no scoped default queryset (e.g. soft-delete) to honor.
            row = Database.fetchone(
                f"SELECT * FROM {cls.table_name} WHERE {cls._pk_col} = ?", [pk]
            )
            if row is None:
                raise RecordNotFoundError(f"No {cls.__name__} matches the given query")
            return cls._from_row(dict(row))
        return cls.get(**{cls._pk_name: pk})

    @classmethod
    async def aget_by_pk(cls, pk: Any) -> Model:
        """Async version of :meth:`get_by_pk`."""
        return await athread(cls.get_by_pk, pk)

    # ------------------------------------------------------------------
    # Upsert
    # ------------------------------------------------------------------

    @classmethod
    def _build_upsert(
        cls,
        kwargs: dict[str, Any],
        conflict_fields: str | Sequence[str] | None,
        update_fields: Sequence[str] | None,
    ) -> tuple[str, list[Any], list[str], Model]:
        instance = cls(**kwargs)
        instance._validate_all()

        if conflict_fields is None:
            if instance.pk is not None:
                conflict_names = [cls._pk_name]
            else:
                conflict_names = [n for n, f in cls._fields.items() if f.unique and n in kwargs][:1]
        elif isinstance(conflict_fields, str):
            conflict_names = [conflict_fields]
        else:
            conflict_names = list(conflict_fields)
        if not conflict_names:
            raise ValueError("upsert() needs conflict_fields or a supplied primary/unique field")

        unknown = [n for n in (*conflict_names, *(update_fields or ())) if n not in cls._fields]
        if unknown:
            raise ValueError(f"Unknown field(s) {unknown!r}")

        d = instance.__dict__
        insert_names = [
            n for n, f in cls._fields.items()
            if not (f.primary_key and d.get(n) is None and isinstance(f, IntegerField))
        ]
        columns = [cls._fields[n].column_name for n in insert_names]
        values = [cls._fields[n].to_db(d.get(n)) for n in insert_names]
        conflict_cols = ", ".join(cls._fields[n].column_name for n in conflict_names)

        if update_fields is None:
            update_names = [n for n in insert_names if n not in conflict_names and not cls._fields[n].primary_key]
        else:
            update_names = list(update_fields)
        if update_names:
            set_sql = ", ".join(
                f"{cls._fields[n].column_name} = excluded.{cls._fields[n].column_name}"
                for n in update_names
            )
            conflict_sql = f"DO UPDATE SET {set_sql}"
        else:
            conflict_sql = "DO NOTHING"

        sql = (
            f"INSERT INTO {cls.table_name} ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)}) ON CONFLICT ({conflict_cols}) "
            f"{conflict_sql} RETURNING {cls._returning_cols}"
        )
        return sql, values, conflict_names, instance

    @classmethod
    def upsert(
        cls,
        *,
        conflict_fields: str | Sequence[str] | None = None,
        update_fields: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> Model:
        """Insert a row or update it on a SQLite conflict target.

        ``conflict_fields`` defaults to the primary key when supplied, or the
        first unique field present in ``kwargs``.
        """
        sql, values, conflict_names, instance = cls._build_upsert(kwargs, conflict_fields, update_fields)
        with Database.transaction():
            row = Database.execute(sql, values).fetchone()
            if row is not None:
                return cls._from_row(dict(row))
            # ON CONFLICT DO NOTHING returns no row; fetch the surviving one.
            return cls.get(**{name: instance.__dict__.get(name) for name in conflict_names})

    @classmethod
    async def aupsert(cls, **kwargs: Any) -> Model:
        """Async version of :meth:`upsert`."""
        return await awrite(functools.partial(cls.upsert, **kwargs))

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    @classmethod
    def _bulk_rows(cls, items: list[dict[str, Any]], validate: bool) -> dict[tuple[str, ...], list[tuple[int, list[Any]]]]:
        """Group items by their column signature for multi-row inserts."""
        groups: dict[tuple[str, ...], list[tuple[int, list[Any]]]] = {}
        errors: list[str] = []
        for idx, item in enumerate(items):
            names: list[str] = []
            values: list[Any] = []
            for name, field in cls._fields.items():
                val = item.get(name, _MISSING)
                if val is _MISSING and field.default is not _MISSING:
                    val = field.default() if callable(field.default) else field.default
                if val is _MISSING:
                    continue
                if validate:
                    try:
                        field.validate(val)
                    except FieldValidationError as exc:
                        errors.append(f"Item {idx}, field '{name}': {exc}")
                        break
                names.append(field.column_name)
                values.append(field.to_db(val))
            else:
                groups.setdefault(tuple(names), []).append((idx, values))
        if errors:
            raise FieldValidationError(
                f"Validation failed for {len(errors)} field(s) in bulk_create:\n" + "\n".join(errors)
            )
        return groups

    @classmethod
    def bulk_create(cls, items: list[dict[str, Any]], *, validate: bool = True) -> list[Model]:
        """Insert many rows efficiently and return the created instances.

        Uses chunked multi-row ``INSERT ... RETURNING`` statements; the
        returned list matches the input order.
        """
        if not items:
            return []
        groups = cls._bulk_rows(items, validate)
        indexed: list[tuple[int, Model]] = []
        with Database.transaction():
            for names, rows in groups.items():
                if not names:
                    for idx, _ in rows:
                        row = Database.execute(
                            f"INSERT INTO {cls.table_name} DEFAULT VALUES RETURNING {cls._returning_cols}"
                        ).fetchone()
                        indexed.append((idx, cls._from_row(dict(row))))
                    continue
                row_marks = f"({', '.join('?' for _ in names)})"
                chunk_size = max(1, 900 // len(names))
                for start in range(0, len(rows), chunk_size):
                    chunk = rows[start:start + chunk_size]
                    sql = (
                        f"INSERT INTO {cls.table_name} ({', '.join(names)}) "
                        f"VALUES {', '.join([row_marks] * len(chunk))} "
                        f"RETURNING {cls._returning_cols}"
                    )
                    flat = [v for _, row in chunk for v in row]
                    returned = Database.execute(sql, flat).fetchall()
                    for (idx, _), row in zip(chunk, returned):
                        indexed.append((idx, cls._from_row(dict(row))))
        indexed.sort(key=lambda pair: pair[0])
        return [instance for _, instance in indexed]

    @classmethod
    async def abulk_create(cls, items: list[dict[str, Any]], *, validate: bool = True) -> list[Model]:
        """Async version of :meth:`bulk_create`."""
        return await awrite(functools.partial(cls.bulk_create, items, validate=validate))

    @classmethod
    def bulk_update(cls, instances: list[Model], fields: list[str] | None = None) -> int:
        """UPDATE many instances in one transaction.  Returns affected row count."""
        if not instances:
            return 0
        update_fields = fields or list(cls._non_pk_field_names)
        set_parts = ", ".join(f"{cls._fields[n].column_name} = ?" for n in update_fields)
        sql = f"UPDATE {cls.table_name} SET {set_parts} WHERE {cls._pk_field.column_name} = ?"
        total = 0
        with Database.transaction() as conn:
            for instance in instances:
                if instance.pk is None:
                    continue
                values = [cls._fields[n].to_db(instance.__dict__.get(n)) for n in update_fields]
                values.append(instance.pk)
                total += conn.execute(sql, values).rowcount
        for instance in instances:
            instance._take_snapshot()
        return total

    @classmethod
    async def abulk_update(cls, instances: list[Model], fields: list[str] | None = None) -> int:
        """Async version of :meth:`bulk_update`."""
        return await awrite(cls.bulk_update, instances, fields)

    # ------------------------------------------------------------------
    # Raw SQL
    # ------------------------------------------------------------------

    @classmethod
    def raw(cls, sql: str, params: Any = None) -> list[Model]:
        """Execute raw SQL returning model instances (columns must match the schema)."""
        return [cls._from_row(dict(r)) for r in Database.fetchall(sql, params)]

    @classmethod
    async def araw(cls, sql: str, params: Any = None) -> list[Model]:
        """Async version of :meth:`raw`."""
        return await athread(cls.raw, sql, params)

    # ------------------------------------------------------------------
    # QuerySet entry point (see also the class-level proxy in MetaModel)
    # ------------------------------------------------------------------

    @classmethod
    def _queryset(cls) -> QuerySet:
        """Return a fresh QuerySet for this model (override to scope defaults)."""
        return QuerySet(cls)


def _toposort_models(models: Sequence[type[Model]]) -> list[type[Model]]:
    """Order models so FK targets come before the models referencing them."""
    ordered = list(models)
    index = {m: i for i, m in enumerate(ordered)}
    selected = set(ordered)
    dependencies: dict[type[Model], set[type[Model]]] = {}
    for model_cls in ordered:
        deps = set()
        for f in model_cls._fields.values():
            if isinstance(f, ForeignKeyField):
                related = f.related_model
                if related is not model_cls and related in selected:
                    deps.add(related)
        dependencies[model_cls] = deps

    result: list[type[Model]] = []
    ready = sorted((m for m, deps in dependencies.items() if not deps), key=index.__getitem__)
    while ready:
        model_cls = ready.pop(0)
        result.append(model_cls)
        for candidate, deps in dependencies.items():
            if model_cls in deps:
                deps.remove(model_cls)
                if not deps and candidate not in result and candidate not in ready:
                    ready.append(candidate)
                    ready.sort(key=index.__getitem__)
    for model_cls in ordered:  # FK cycles fall back to declaration order
        if model_cls not in result:
            result.append(model_cls)
    return result


def registered_models() -> list[type[Model]]:
    """All registered model classes in FK-dependency order."""
    return _toposort_models(list(_model_registry.values()))


def create_all(models: Iterable[type[Model]] | None = None, *, if_not_exists: bool = True) -> None:
    """Create tables for *models* (default: every registered model) in FK order."""
    targets = _toposort_models(list(models)) if models is not None else registered_models()
    for model_cls in targets:
        model_cls.create_table(if_not_exists)


async def acreate_all(models: Iterable[type[Model]] | None = None, *, if_not_exists: bool = True) -> None:
    """Async version of :func:`create_all`."""
    await awrite(functools.partial(create_all, models, if_not_exists=if_not_exists))


def drop_all(models: Iterable[type[Model]] | None = None) -> None:
    """Drop tables for *models* (default: every registered model), children first."""
    targets = _toposort_models(list(models)) if models is not None else registered_models()
    for model_cls in reversed(targets):
        model_cls.drop_table()


async def adrop_all(models: Iterable[type[Model]] | None = None) -> None:
    """Async version of :func:`drop_all`."""
    await awrite(functools.partial(drop_all, models))
