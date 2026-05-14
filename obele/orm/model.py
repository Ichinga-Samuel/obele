"""obele model base class and metaclass with sync and async APIs.

Provides the ``Model`` class that maps Python classes to SQLite tables with
full CRUD support, schema management, and query-builder integration.

Async methods are prefixed with ``a`` (e.g. ``acreate``, ``asave``)::

    # Sync
    users = User.filter(age__gt=18).order_by("-name").limit(10).all()
    # Async
    users = await User.filter(age__gt=18).order_by("-name").limit(10).aall()
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, ClassVar

from .database import Database
from .fields import Field, IntegerField, ForeignKeyField, _MISSING
from .query import QuerySet
from .exceptions import RecordNotFoundError, FieldValidationError, MigrationError
from .signals import pre_save, post_save, pre_delete, post_delete, pre_create, post_create
from .sql import validate_identifier

# Global registry so ForeignKeyField can resolve string references lazily.
_model_registry: dict[str, type[Model]] = {}


# ============================================================================
# Reverse relation support
# ============================================================================

class ReverseRelationManager:
    """Query helper for reverse foreign-key access.

    Returned by the ``ReverseRelationDescriptor``; supports the full
    QuerySet API plus convenience helpers like ``create()``::

        user.posts.all()
        user.posts.filter(published=True).count()
        user.posts.create(title="New Post")
    """

    __slots__ = ("instance", "related_model", "field_name")

    def __init__(self, instance: Model, related_model: type[Model], field_name: str) -> None:
        self.instance = instance
        self.related_model = related_model
        self.field_name = field_name

    def __repr__(self) -> str:
        pk = self.instance.__dict__.get(self.instance._pk_name)
        return (
            f"<ReverseRelationManager owner={type(self.instance).__name__} "
            f"related={self.related_model.__name__} field={self.field_name!r} pk={pk}>"
        )

    def _queryset(self) -> QuerySet:
        pk = self.instance.__dict__.get(self.instance._pk_name)
        if pk is None:
            raise RecordNotFoundError("Cannot use a reverse relation on an unsaved instance")
        return self.related_model.filter(**{self.field_name: pk})

    # Delegate all QuerySet methods dynamically
    def __getattr__(self, name: str) -> Any:
        qs = self._queryset()
        attr = getattr(qs, name, None)
        if attr is not None:
            return attr
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")

    def create(self, **kwargs: Any) -> Model:
        """Create a related object with the FK pre-set to this instance."""
        kwargs[self.field_name] = self.instance
        return self.related_model.create(**kwargs)

    async def acreate(self, **kwargs: Any) -> Model:
        """Async version of :meth:`create`."""
        kwargs[self.field_name] = self.instance
        return await self.related_model.acreate(**kwargs)

    def __iter__(self):
        return iter(self._queryset())

    async def __aiter__(self):
        async for item in self._queryset():
            yield item


class ReverseRelationDescriptor:
    """Descriptor installed on a target Model by the metaclass.

    On attribute access it returns a :class:`ReverseRelationManager`
    bound to the calling instance.
    """

    __slots__ = ("related_model", "field_name", "accessor_name")

    def __init__(self, related_model: type[Model], field_name: str, accessor_name: str) -> None:
        self.related_model = related_model
        self.field_name = field_name
        self.accessor_name = accessor_name

    def __get__(self, instance: Model | None, owner: type[Model]) -> Any:
        if instance is None:
            return self
        return ReverseRelationManager(instance, self.related_model, self.field_name)


def _register_reverse_relations() -> None:
    """Scan the registry and install reverse relation descriptors."""
    for source_model in _model_registry.values():
        for field_name, field in source_model._fields.items():
            if not isinstance(field, ForeignKeyField):
                continue
            try:
                related_model = field.related_model
            except Exception:
                continue

            accessor_name = field.related_name or f"{source_model.__name__.lower()}_set"
            reverse_relations = getattr(related_model, "_reverse_relations", {})
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
            reverse_relations[accessor_name] = descriptor
            related_model._reverse_relations = reverse_relations


# ============================================================================
# MetaModel
# ============================================================================

class MetaModel(type):
    """Collect ``Field`` descriptors and configure the Model class."""

    def __new__(mcs, cls_name: str, bases: tuple[type, ...], namespace: dict[str, Any]) -> type:
        fields: dict[str, Field] = {}

        # Inherit fields from parent models
        for base in bases:
            if hasattr(base, "_fields"):
                fields.update(base._fields)

        # Collect fields from mixin bases (non-Model classes with Field attrs)
        for base in bases:
            if not hasattr(base, "_fields"):
                for attr_name in dir(base):
                    attr_val = getattr(base, attr_name, None)
                    if isinstance(attr_val, Field):
                        fields[attr_name] = attr_val

        # Collect fields defined in this class
        for attr_name, attr_val in list(namespace.items()):
            if isinstance(attr_val, Field):
                fields[attr_name] = attr_val

        namespace["_fields"] = fields
        namespace.setdefault("_reverse_relations", {})

        # Default table_name = class name lower-cased (skip for base Model)
        if "table_name" not in namespace or not namespace.get("table_name"):
            namespace["table_name"] = cls_name.lower()
        validate_identifier(namespace["table_name"], kind="table name")

        cls = super().__new__(mcs, cls_name, bases, namespace)

        # Find primary key field
        pk_field = None
        for field in fields.values():
            if field.primary_key:
                pk_field = field
                break

        # Auto-add an `id` primary key if none was declared (skip base Model)
        if pk_field is None and any(hasattr(b, "_fields") for b in bases):
            pk = IntegerField(primary_key=True)
            pk.__set_name__(cls, "id")
            fields["id"] = pk
            setattr(cls, "id", pk)
            pk_field = pk

        cls._pk_field = pk_field
        cls._pk_name = pk_field.attr_name if pk_field else "id"

        # Pre-compute SQL templates for INSERT/UPDATE
        if any(hasattr(b, "_fields") for b in bases):
            mcs._cache_sql_templates(cls)
            mcs._collect_constraints(cls)

        # Register for FK lazy resolution + reverse relations
        if cls_name != "Model":
            _model_registry[cls_name] = cls
            _register_reverse_relations()

        return cls

    @staticmethod
    def _cache_sql_templates(cls: type) -> None:
        """Pre-build INSERT and UPDATE SQL templates for this model."""
        non_pk = {n: f for n, f in cls._fields.items() if not f.primary_key}
        if not non_pk:
            cls._insert_sql = f"INSERT INTO {cls.table_name} DEFAULT VALUES"
            cls._update_sql = ""
            cls._non_pk_field_names = ()
            return

        columns = [f.column_name for f in non_pk.values()]
        placeholders = ", ".join("?" for _ in columns)
        cls._insert_sql = (
            f"INSERT INTO {cls.table_name} ({', '.join(columns)}) "
            f"VALUES ({placeholders})"
        )

        set_clause = ", ".join(f"{col} = ?" for col in columns)
        pk_col = cls._pk_field.column_name if cls._pk_field else "id"
        cls._update_sql = f"UPDATE {cls.table_name} SET {set_clause} WHERE {pk_col} = ?"

        cls._non_pk_field_names = tuple(non_pk.keys())

    @staticmethod
    def _collect_constraints(cls: type) -> None:
        """Collect table-level constraint specs from the class definition."""
        cls._unique_together = getattr(cls, 'unique_together', [])
        cls._index_together = getattr(cls, 'index_together', [])
        cls._check_constraints = getattr(cls, 'check_constraints', [])


# ============================================================================
# Model
# ============================================================================

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
    """

    table_name: ClassVar[str] = ""
    unique_together: ClassVar[list[tuple[str, ...]]] = []
    index_together: ClassVar[list[tuple[str, ...]]] = []
    check_constraints: ClassVar[list[str]] = []
    _fields: ClassVar[dict[str, Field]]
    _pk_field: ClassVar[Field | None]
    _pk_name: ClassVar[str]
    _reverse_relations: ClassVar[dict[str, ReverseRelationDescriptor]]
    _insert_sql: ClassVar[str]
    _update_sql: ClassVar[str]
    _non_pk_field_names: ClassVar[tuple[str, ...]]
    _unique_together: ClassVar[list[tuple[str, ...]]]
    _index_together: ClassVar[list[tuple[str, ...]]]
    _check_constraints: ClassVar[list[str]]

    def __init__(self, **kwargs: Any) -> None:
        for name, field in self._fields.items():
            if name in kwargs:
                setattr(self, name, kwargs[name])
            elif field.default is not _MISSING:
                default = field.default() if callable(field.default) else field.default
                setattr(self, name, default)
            elif field.primary_key:
                # PK is None until the row is inserted
                self.__dict__[name] = None

        self._persisted = kwargs.get("_persisted", False)
        self._annotations: dict[str, Any] = kwargs.get("_annotations", {})
        # Snapshot for dirty tracking
        self._snapshot: dict[str, Any] = {}
        if self._persisted:
            self._take_snapshot()

    def _take_snapshot(self) -> None:
        """Snapshot current field values for dirty tracking."""
        self._snapshot = {
            name: self.__dict__.get(name) for name in self._non_pk_field_names
        }

    @property
    def dirty_fields(self) -> dict[str, Any]:
        """Return a dict of field names that have changed since load/last save."""
        if not self._persisted:
            return {n: self.__dict__.get(n) for n in self._non_pk_field_names}
        return {
            name: self.__dict__.get(name)
            for name in self._non_pk_field_names
            if self.__dict__.get(name) != self._snapshot.get(name)
        }

    @property
    def is_dirty(self) -> bool:
        """Return True if any fields have changed since load/last save."""
        return bool(self.dirty_fields)

    def __repr__(self) -> str:
        pk = self.__dict__.get(self._pk_name)
        return f"<{type(self).__name__} pk={pk}>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, type(self)):
            return NotImplemented
        pk = self.__dict__.get(self._pk_name)
        other_pk = other.__dict__.get(other._pk_name)
        if pk is None or other_pk is None:
            return self is other
        return pk == other_pk

    def __hash__(self) -> int:
        return hash((type(self).__name__, self.__dict__.get(self._pk_name)))

    # ---- Schema helpers ---------------------------------------------------

    @classmethod
    def _column_ddls(cls) -> list[str]:
        return [field.column_ddl() for field in cls._fields.values()]

    @classmethod
    def _table_constraints_ddl(cls) -> list[str]:
        """Return table-level constraint DDL fragments."""
        constraints: list[str] = []
        for fields in getattr(cls, '_unique_together', []):
            cols = ", ".join(
                cls._fields[f].column_name
                for f in fields
                if f in cls._fields
            )
            if cols:
                constraints.append(f"UNIQUE ({cols})")
        for expr in getattr(cls, '_check_constraints', []):
            constraints.append(f"CHECK ({expr})")
        return constraints

    @classmethod
    def _create_table_sql(cls, if_not_exists: bool = True) -> str:
        maybe = "IF NOT EXISTS " if if_not_exists else ""
        parts = cls._column_ddls() + cls._table_constraints_ddl()
        return f"CREATE TABLE {maybe}{cls.table_name} ({', '.join(parts)})"

    @classmethod
    def _create_index_sqls(cls) -> list[str]:
        indexes = [
            f"CREATE INDEX IF NOT EXISTS idx_{cls.table_name}_{f.column_name} "
            f"ON {cls.table_name} ({f.column_name})"
            for f in cls._fields.values()
            if f.index and not f.primary_key
        ]
        # Compound indexes from index_together
        for i, fields in enumerate(getattr(cls, '_index_together', [])):
            cols = ", ".join(
                cls._fields[f].column_name
                for f in fields
                if f in cls._fields
            )
            if cols:
                suffix = "_".join(
                    cls._fields[f].column_name
                    for f in fields
                    if f in cls._fields
                )
                indexes.append(
                    f"CREATE INDEX IF NOT EXISTS idx_{cls.table_name}_{suffix} "
                    f"ON {cls.table_name} ({cols})"
                )
        return indexes

    @classmethod
    def create_table(cls, if_not_exists: bool = True) -> None:
        """Create the SQLite table for this model."""
        Database.execute(cls._create_table_sql(if_not_exists))
        for sql in cls._create_index_sqls():
            Database.execute(sql)

    @classmethod
    async def acreate_table(cls, if_not_exists: bool = True) -> None:
        """Async version of :meth:`create_table`."""
        await asyncio.to_thread(cls.create_table, if_not_exists)

    @classmethod
    def drop_table(cls, if_exists: bool = True) -> None:
        """Drop the SQLite table for this model."""
        maybe = "IF EXISTS " if if_exists else ""
        Database.execute(f"DROP TABLE {maybe}{cls.table_name}")

    @classmethod
    async def adrop_table(cls, if_exists: bool = True) -> None:
        """Async version of :meth:`drop_table`."""
        await asyncio.to_thread(cls.drop_table, if_exists)

    # ---- Migration --------------------------------------------------------

    @classmethod
    def _table_exists(cls) -> bool:
        return (
            Database.fetchone(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                [cls.table_name],
            )
            is not None
        )

    @classmethod
    def _existing_columns(cls) -> set[str]:
        return {
            row["name"]
            for row in Database.fetchall(f"PRAGMA table_info({cls.table_name})")
        }

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
    def migrate(
        cls,
        *,
        rename_fields: dict[str, str] | None = None,
        create_if_missing: bool = True,
    ) -> None:
        """SQLite-only schema migration by safely rebuilding the table.

        Handles column additions, removals, and renames in a single
        transaction using the model definition as the source of truth.

        Args:
            rename_fields: Map of ``{new_field_name: old_column_name}``.
            create_if_missing: If ``True`` (default), create the table
                if it doesn't already exist.
        """
        if not cls._table_exists():
            if create_if_missing:
                cls.create_table()
                return
            raise MigrationError(f"Table '{cls.table_name}' does not exist")

        rename_fields = rename_fields or {}
        existing_columns = cls._existing_columns()
        temp_table = f"{cls.table_name}__old"

        insert_columns: list[str] = []
        select_expressions: list[str] = []
        params: list[Any] = []

        for field in cls._fields.values():
            new_column = field.column_name
            old_column = rename_fields.get(new_column, new_column)
            if old_column in existing_columns:
                insert_columns.append(new_column)
                select_expressions.append(old_column)
                continue

            default_value = cls._migration_default_value(field)
            if default_value is _MISSING:
                continue

            insert_columns.append(new_column)
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
    async def amigrate(
        cls,
        *,
        rename_fields: dict[str, str] | None = None,
        create_if_missing: bool = True,
    ) -> None:
        """Async version of :meth:`migrate`."""
        await asyncio.to_thread(
            cls.migrate,
            rename_fields=rename_fields,
            create_if_missing=create_if_missing,
        )

    # ---- CRUD -------------------------------------------------------------

    def save(self) -> None:
        """Insert or update this instance in the database.

        Uses dirty tracking: only changed fields are sent in UPDATE statements.
        Emits ``pre_save`` / ``post_save`` signals.
        """
        pk_value = self.__dict__.get(self._pk_name)
        for name, field in self._fields.items():
            value = self.__dict__.get(name)
            field.validate(value)
        created = not self._persisted or pk_value is None
        pre_save.send(type(self), instance=self, created=created)
        if created:
            pre_create.send(type(self), instance=self)
        if self._persisted and pk_value is not None:
            self._update()
        else:
            self._insert()
        self._take_snapshot()
        post_save.send(type(self), instance=self, created=created)
        if created:
            post_create.send(type(self), instance=self)

    async def asave(self) -> None:
        """Async version of :meth:`save`."""
        await asyncio.to_thread(self.save)

    def _insert(self) -> None:
        fields = self._fields
        insert_names = [
            name for name, field in fields.items()
            if not (
                field.primary_key
                and self.__dict__.get(name) is None
                and isinstance(field, IntegerField)
            )
        ]
        if insert_names:
            columns = [fields[n].column_name for n in insert_names]
            placeholders = ", ".join("?" for _ in columns)
            sql = (
                f"INSERT INTO {self.table_name} ({', '.join(columns)}) "
                f"VALUES ({placeholders})"
            )
            values = [fields[n].to_db(self.__dict__.get(n)) for n in insert_names]
            cursor = Database.execute(sql, values)
        else:
            cursor = Database.execute(f"INSERT INTO {self.table_name} DEFAULT VALUES")
        if self.__dict__.get(self._pk_name) is None:
            self.__dict__[self._pk_name] = cursor.lastrowid
        self._persisted = True

    def _update(self) -> None:
        dirty = self.dirty_fields
        if not dirty:
            return  # Nothing changed, skip the query

        fields = self._fields
        set_parts = []
        values = []
        for name, value in dirty.items():
            f = fields[name]
            set_parts.append(f"{f.column_name} = ?")
            values.append(f.to_db(value))

        pk_value = self.__dict__[self._pk_name]
        values.append(pk_value)
        pk_col = type(self)._pk_field.column_name
        sql = f"UPDATE {self.table_name} SET {', '.join(set_parts)} WHERE {pk_col} = ?"
        Database.execute(sql, values)

    def delete(self) -> None:
        """Delete this instance from the database.

        Emits ``pre_delete`` / ``post_delete`` signals.
        """
        pk_value = self.__dict__.get(self._pk_name)
        if pk_value is None:
            raise RecordNotFoundError("Cannot delete an unsaved instance")
        pre_delete.send(type(self), instance=self)
        pk_col = type(self)._pk_field.column_name
        Database.execute(
            f"DELETE FROM {self.table_name} WHERE {pk_col} = ?",
            [pk_value],
        )
        self.__dict__[self._pk_name] = None
        self._persisted = False
        post_delete.send(type(self), instance=self)

    async def adelete(self) -> None:
        """Async version of :meth:`delete`."""
        await asyncio.to_thread(self.delete)

    def refresh(self) -> None:
        """Re-read this instance's data from the database."""
        pk_value = self.__dict__.get(self._pk_name)
        if pk_value is None:
            raise RecordNotFoundError("Cannot refresh an unsaved instance")
        pk_col = type(self)._pk_field.column_name
        row = Database.fetchone(
            f"SELECT * FROM {self.table_name} WHERE {pk_col} = ?",
            [pk_value],
        )
        if row is None:
            raise RecordNotFoundError(
                f"{type(self).__name__} with pk={pk_value} not found"
            )
        row_dict = dict(row)
        for name, field in self._fields.items():
            if field.column_name in row_dict:
                raw = row_dict[field.column_name]
                self.__dict__[name] = field.to_python(raw) if raw is not None else None
                # Clear FK caches on refresh
                if isinstance(field, ForeignKeyField):
                    self.__dict__.pop(field.cache_attr_name, None)
        self._take_snapshot()

    async def arefresh(self) -> None:
        """Async version of :meth:`refresh`."""
        await asyncio.to_thread(self.refresh)

    # ---- Serialization ----------------------------------------------------

    def to_dict(
        self,
        *,
        mode: str = "python",
        include_annotations: bool = True,
    ) -> dict[str, Any]:
        """Serialize all fields to a dictionary.

        Args:
            mode: ``"python"`` for raw Python values, ``"db"`` for
                database-serialized values.
            include_annotations: If ``True``, merge annotation values.
        """
        if mode == "python":
            data = {name: self.__dict__.get(name) for name in self._fields}
        elif mode == "db":
            data = {
                name: field.to_db(self.__dict__.get(name))
                for name, field in self._fields.items()
            }
        else:
            raise ValueError("mode must be 'python' or 'db'")

        if include_annotations:
            data.update(self._annotations)
        return data

    def to_db_dict(self, *, include_annotations: bool = True) -> dict[str, Any]:
        """Shorthand for ``to_dict(mode="db")``."""
        return self.to_dict(mode="db", include_annotations=include_annotations)

    # ---- Construction from DB rows ----------------------------------------

    @classmethod
    def _from_row(cls, row_dict: dict[str, Any], *, annotations: dict[str, Any] | None = None) -> Model:
        """Construct a model instance from a database row dict."""
        instance = cls.__new__(cls)
        d = instance.__dict__
        for name, field in cls._fields.items():
            col = field.column_name
            if col in row_dict:
                raw = row_dict[col]
                d[name] = field.to_python(raw) if raw is not None else None
        d["_persisted"] = True
        ann = annotations or {}
        d["_annotations"] = ann
        d["_snapshot"] = {name: d.get(name) for name in cls._non_pk_field_names}
        for alias, value in ann.items():
            d[alias] = value
        return instance

    # ---- Create / get_or_create / update_or_create ------------------------

    @classmethod
    def create(cls, **kwargs: Any) -> Model:
        """Create, save, and return a new instance."""
        instance = cls(**kwargs)
        instance.save()
        return instance

    @classmethod
    async def acreate(cls, **kwargs: Any) -> Model:
        """Async version of :meth:`create`."""
        return await asyncio.to_thread(cls.create, **kwargs)

    @classmethod
    def get_or_create(cls, defaults: dict[str, Any] | None = None, **kwargs: Any) -> tuple[Model, bool]:
        """Return ``(instance, created)`` - fetch if it exists, else create."""
        with Database.transaction():
            try:
                instance = cls.get(**kwargs)
                return instance, False
            except RecordNotFoundError:
                if defaults:
                    kwargs.update(defaults)
                return cls.create(**kwargs), True

    @classmethod
    async def aget_or_create(cls, defaults: dict[str, Any] | None = None, **kwargs: Any) -> tuple[Model, bool]:
        """Async version of :meth:`get_or_create`."""
        return await asyncio.to_thread(cls.get_or_create, defaults, **kwargs)

    @classmethod
    def update_or_create(cls, defaults: dict[str, Any] | None = None, **kwargs: Any) -> tuple[Model, bool]:
        """Fetch and update if exists, otherwise create.

        Returns ``(instance, created)`` where *created* is ``True`` if a new
        row was inserted.
        """
        defaults = defaults or {}
        with Database.transaction():
            try:
                instance = cls.get(**kwargs)
                for key, val in defaults.items():
                    setattr(instance, key, val)
                instance.save()
                return instance, False
            except RecordNotFoundError:
                kwargs.update(defaults)
                return cls.create(**kwargs), True

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
        return await asyncio.to_thread(cls.get_or_none, **kwargs)

    @classmethod
    def get_by_pk(cls, pk: Any) -> Model:
        """Fetch one instance by primary key."""
        return cls.get(**{cls._pk_name: pk})

    @classmethod
    async def aget_by_pk(cls, pk: Any) -> Model:
        """Async version of :meth:`get_by_pk`."""
        return await asyncio.to_thread(cls.get_by_pk, pk)

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
        instance = cls(**kwargs)
        for name, field in cls._fields.items():
            field.validate(instance.__dict__.get(name))

        if conflict_fields is None:
            pk_value = instance.__dict__.get(cls._pk_name)
            if pk_value is not None:
                conflict_names = [cls._pk_name]
            else:
                conflict_names = [
                    name for name, field in cls._fields.items()
                    if field.unique and name in kwargs
                ][:1]
        elif isinstance(conflict_fields, str):
            conflict_names = [conflict_fields]
        else:
            conflict_names = list(conflict_fields)
        if not conflict_names:
            raise ValueError("upsert() needs conflict_fields or a supplied primary/unique field")
        for name in conflict_names:
            if name not in cls._fields:
                raise ValueError(f"Unknown conflict field {name!r}")

        insert_names = [
            name for name, field in cls._fields.items()
            if name in kwargs
            or field.default is not _MISSING
            or not (
                field.primary_key
                and instance.__dict__.get(name) is None
                and isinstance(field, IntegerField)
            )
        ]
        columns = [cls._fields[n].column_name for n in insert_names]
        values = [cls._fields[n].to_db(instance.__dict__.get(n)) for n in insert_names]
        placeholders = ", ".join("?" for _ in columns)
        conflict_cols = ", ".join(cls._fields[n].column_name for n in conflict_names)

        if update_fields is None:
            update_names = [
                n for n in insert_names
                if n not in conflict_names and not cls._fields[n].primary_key
            ]
        else:
            update_names = list(update_fields)
        for name in update_names:
            if name not in cls._fields:
                raise ValueError(f"Unknown update field {name!r}")

        returning_cols = ", ".join(field.column_name for field in cls._fields.values())
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
            f"VALUES ({placeholders}) ON CONFLICT({conflict_cols}) "
            f"{conflict_sql} RETURNING {returning_cols}"
        )
        with Database.transaction() as conn:
            cursor = conn.execute(sql, values)
            row = cursor.fetchone()
        if row is not None:
            return cls._from_row(dict(row))
        return cls.get(**{name: instance.__dict__.get(name) for name in conflict_names})

    @classmethod
    async def aupsert(
        cls,
        *,
        conflict_fields: str | Sequence[str] | None = None,
        update_fields: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> Model:
        """Async version of :meth:`upsert`."""
        return await asyncio.to_thread(
            cls.upsert,
            conflict_fields=conflict_fields,
            update_fields=update_fields,
            **kwargs,
        )

    @classmethod
    async def aupdate_or_create(cls, defaults: dict[str, Any] | None = None, **kwargs: Any) -> tuple[Model, bool]:
        """Async version of :meth:`update_or_create`."""
        return await asyncio.to_thread(cls.update_or_create, defaults, **kwargs)

    # ---- Bulk operations --------------------------------------------------

    @classmethod
    def bulk_create(cls, items: list[dict[str, Any]], *, validate: bool = True) -> list[Model]:
        """Insert many rows efficiently and return the instances.

        Uses ``INSERT ... RETURNING`` (SQLite 3.35+) for efficient retrieval.
        """
        if not items:
            return []

        non_pk = {n: f for n, f in cls._fields.items() if not f.primary_key}
        columns = [f.column_name for f in non_pk.values()]
        placeholders = ", ".join("?" for _ in columns)

        errors: list[str] = []
        params_seq = []
        for idx, item in enumerate(items):
            row_values = []
            for name, field in non_pk.items():
                val = item.get(name)
                if val is None and field.default is not _MISSING:
                    val = field.default() if callable(field.default) else field.default
                if validate:
                    try:
                        field.validate(val)
                    except FieldValidationError as exc:
                        errors.append(f"Item {idx}, field '{name}': {exc}")
                row_values.append(field.to_db(val))
            params_seq.append(row_values)

        if errors:
            raise FieldValidationError(
                f"Validation failed for {len(errors)} field(s) in bulk_create:\n"
                + "\n".join(errors)
            )

        # Use RETURNING for efficient row retrieval (SQLite 3.35+)
        returning_cols = ", ".join(f.column_name for f in cls._fields.values())
        sql = (
            f"INSERT INTO {cls.table_name} ({', '.join(columns)}) "
            f"VALUES ({placeholders}) RETURNING {returning_cols}"
        )

        instances = []
        with Database.transaction() as conn:
            for row_params in params_seq:
                cursor = conn.execute(sql, row_params)
                row = cursor.fetchone()
                if row is not None:
                    instances.append(cls._from_row(dict(row)))

        return instances

    @classmethod
    async def abulk_create(cls, items: list[dict[str, Any]], *, validate: bool = True) -> list[Model]:
        """Async version of :meth:`bulk_create`."""
        return await asyncio.to_thread(cls.bulk_create, items, validate=validate)

    @classmethod
    def bulk_update(
        cls,
        instances: list[Model],
        fields: list[str] | None = None,
    ) -> int:
        """Bulk UPDATE a list of model instances.

        Args:
            instances: Model instances to update (must be persisted).
            fields: Specific field names to update. If ``None``, updates
                all non-PK fields.

        Returns:
            Number of rows affected.
        """
        if not instances:
            return 0

        update_fields = fields or list(cls._non_pk_field_names)
        pk_col = cls._pk_field.column_name

        total = 0
        with Database.transaction() as conn:
            for instance in instances:
                pk_value = instance.__dict__.get(instance._pk_name)
                if pk_value is None:
                    continue
                set_parts = []
                values = []
                for name in update_fields:
                    f = cls._fields[name]
                    set_parts.append(f"{f.column_name} = ?")
                    values.append(f.to_db(instance.__dict__.get(name)))
                if not set_parts:
                    continue
                values.append(pk_value)
                sql = f"UPDATE {cls.table_name} SET {', '.join(set_parts)} WHERE {pk_col} = ?"
                cursor = conn.execute(sql, values)
                total += cursor.rowcount

        for instance in instances:
            instance._take_snapshot()
        return total

    @classmethod
    async def abulk_update(cls, instances: list[Model], fields: list[str] | None = None) -> int:
        """Async version of :meth:`bulk_update`."""
        return await asyncio.to_thread(cls.bulk_update, instances, fields)

    # ---- Raw SQL ----------------------------------------------------------

    @classmethod
    def raw(cls, sql: str, params: Any = None) -> list[Model]:
        """Execute raw SQL and return model instances.

        The SQL must return columns matching the model's field column names.
        """
        rows = Database.fetchall(sql, params)
        return [cls._from_row(dict(r)) for r in rows]

    @classmethod
    async def araw(cls, sql: str, params: Any = None) -> list[Model]:
        """Async version of :meth:`raw`."""
        return await asyncio.to_thread(cls.raw, sql, params)

    # ---- QuerySet bridge ---------------------------------------------------

    @classmethod
    def _queryset(cls) -> QuerySet:
        """Return a fresh QuerySet for this model."""
        return QuerySet(cls)

    @classmethod
    def filter(cls, *conditions: Any, **kwargs: Any) -> QuerySet:
        return cls._queryset().filter(*conditions, **kwargs)

    @classmethod
    def exclude(cls, *conditions: Any, **kwargs: Any) -> QuerySet:
        return cls._queryset().exclude(*conditions, **kwargs)

    @classmethod
    def order_by(cls, *fields: str) -> QuerySet:
        return cls._queryset().order_by(*fields)

    @classmethod
    def limit(cls, n: int) -> QuerySet:
        return cls._queryset().limit(n)

    @classmethod
    def offset(cls, n: int) -> QuerySet:
        return cls._queryset().offset(n)

    @classmethod
    def select_related(cls, *fk_fields: str) -> QuerySet:
        return cls._queryset().select_related(*fk_fields)

    @classmethod
    def join(cls, relation_name: str, *, join_type: str = "INNER") -> QuerySet:
        return cls._queryset().join(relation_name, join_type=join_type)

    @classmethod
    def annotate(cls, **annotations: Any) -> QuerySet:
        return cls._queryset().annotate(**annotations)

    @classmethod
    def values(cls, *fields: str) -> QuerySet:
        return cls._queryset().values(*fields)

    @classmethod
    def values_list(cls, *fields: str, flat: bool = False) -> QuerySet:
        return cls._queryset().values_list(*fields, flat=flat)

    @classmethod
    def only(cls, *fields: str) -> QuerySet:
        return cls._queryset().only(*fields)

    @classmethod
    def defer(cls, *fields: str) -> QuerySet:
        return cls._queryset().defer(*fields)

    @classmethod
    def distinct(cls) -> QuerySet:
        return cls._queryset().distinct()

    @classmethod
    def group_by(cls, *fields: str) -> QuerySet:
        return cls._queryset().group_by(*fields)

    @classmethod
    def iterator(cls, chunk_size: int = 2000):
        return cls._queryset().iterator(chunk_size=chunk_size)

    @classmethod
    def all(cls) -> list[Model]:
        return cls._queryset().all()

    @classmethod
    async def aall(cls) -> list[Model]:
        return await cls._queryset().aall()

    @classmethod
    def first(cls) -> Model | None:
        return cls._queryset().first()

    @classmethod
    async def afirst(cls) -> Model | None:
        return await cls._queryset().afirst()

    @classmethod
    def get(cls, **kwargs: Any) -> Model:
        return cls._queryset().get(**kwargs)

    @classmethod
    async def aget(cls, **kwargs: Any) -> Model:
        return await cls._queryset().aget(**kwargs)

    @classmethod
    def count(cls) -> int:
        return cls._queryset().count()

    @classmethod
    async def acount(cls) -> int:
        return await cls._queryset().acount()

    @classmethod
    def exists(cls) -> bool:
        return cls._queryset().exists()

    @classmethod
    async def aexists(cls) -> bool:
        return await cls._queryset().aexists()

    @classmethod
    def aggregate(cls, func: str, field: str) -> Any:
        return cls._queryset().aggregate(func, field)

    @classmethod
    async def aaggregate(cls, func: str, field: str) -> Any:
        return await cls._queryset().aaggregate(func, field)
