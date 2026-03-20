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
from typing import Any, ClassVar

from .database import Database
from .fields import Field, IntegerField, ForeignKeyField, _MISSING
from .query import QuerySet
from .exceptions import RecordNotFoundError, FieldValidationError

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

    def __init__(
        self,
        instance: Model,
        related_model: type[Model],
        field_name: str,
    ) -> None:
        self.instance = instance
        self.related_model = related_model
        self.field_name = field_name

    def __repr__(self) -> str:
        pk_value = self.instance.__dict__.get(self.instance._pk_name)
        return (
            f"<ReverseRelationManager owner={type(self.instance).__name__} "
            f"related={self.related_model.__name__} field={self.field_name!r} pk={pk_value}>"
        )

    def _queryset(self) -> QuerySet:
        pk_value = self.instance.__dict__.get(self.instance._pk_name)
        if pk_value is None:
            raise RecordNotFoundError("Cannot use a reverse relation on an unsaved instance")
        return self.related_model.filter(**{self.field_name: pk_value})

    def queryset(self) -> QuerySet:
        """Return the underlying ``QuerySet`` for further chaining."""
        return self._queryset()

    def all(self) -> list[Model]:
        return self._queryset().all()

    async def aall(self) -> list[Model]:
        return await self._queryset().aall()

    def filter(self, *conditions: Any, **kwargs: Any) -> QuerySet:
        return self._queryset().filter(*conditions, **kwargs)

    def exclude(self, *conditions: Any, **kwargs: Any) -> QuerySet:
        return self._queryset().exclude(*conditions, **kwargs)

    def order_by(self, *fields: str) -> QuerySet:
        return self._queryset().order_by(*fields)

    def limit(self, n: int) -> QuerySet:
        return self._queryset().limit(n)

    def offset(self, n: int) -> QuerySet:
        return self._queryset().offset(n)

    def count(self) -> int:
        return self._queryset().count()

    async def acount(self) -> int:
        return await self._queryset().acount()

    def exists(self) -> bool:
        return self._queryset().exists()

    async def aexists(self) -> bool:
        return await self._queryset().aexists()

    def first(self) -> Model | None:
        return self._queryset().first()

    async def afirst(self) -> Model | None:
        return await self._queryset().afirst()

    def get(self, **kwargs: Any) -> Model:
        return self._queryset().get(**kwargs)

    async def aget(self, **kwargs: Any) -> Model:
        return await self._queryset().aget(**kwargs)

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

    def __init__(
        self,
        related_model: type[Model],
        field_name: str,
        accessor_name: str,
    ) -> None:
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

    def __new__(
        mcs,
        cls_name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
    ) -> type:
        fields: dict[str, Field] = {}

        # Inherit fields from parent models
        for base in bases:
            if hasattr(base, "_fields"):
                fields.update(base._fields)

        # Collect fields defined in this class
        for attr_name, attr_val in list(namespace.items()):
            if isinstance(attr_val, Field):
                fields[attr_name] = attr_val

        namespace["_fields"] = fields
        namespace.setdefault("_reverse_relations", {})

        # Default table_name = class name lower-cased (skip for base Model)
        if "table_name" not in namespace or not namespace.get("table_name"):
            namespace["table_name"] = cls_name.lower()

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

        # Register for FK lazy resolution + reverse relations
        if cls_name != "Model":
            _model_registry[cls_name] = cls
            _register_reverse_relations()

        return cls


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
    _fields: ClassVar[dict[str, Field]]
    _pk_field: ClassVar[Field | None]
    _pk_name: ClassVar[str]
    _reverse_relations: ClassVar[dict[str, ReverseRelationDescriptor]]

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

        # Track whether this instance has been persisted
        self._persisted = kwargs.get("_persisted", False)
        self._annotations: dict[str, Any] = kwargs.get("_annotations", {})

    def __repr__(self) -> str:
        pk = self.__dict__.get(self._pk_name)
        return f"<{type(self).__name__} pk={pk}>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, type(self)):
            return NotImplemented
        return self.__dict__.get(self._pk_name) == other.__dict__.get(other._pk_name)

    def __hash__(self) -> int:
        return hash((type(self).__name__, self.__dict__.get(self._pk_name)))

    # ---- Schema helpers ---------------------------------------------------

    @classmethod
    def _column_ddls(cls) -> list[str]:
        return [field.column_ddl() for field in cls._fields.values()]

    @classmethod
    def _create_table_sql(cls, if_not_exists: bool = True) -> str:
        maybe = "IF NOT EXISTS " if if_not_exists else ""
        return f"CREATE TABLE {maybe}{cls.table_name} ({', '.join(cls._column_ddls())})"

    @classmethod
    def _create_index_sqls(cls) -> list[str]:
        sqls: list[str] = []
        for field in cls._fields.values():
            if field.index and not field.primary_key:
                sqls.append(
                    f"CREATE INDEX IF NOT EXISTS idx_{cls.table_name}_{field.column_name} "
                    f"ON {cls.table_name} ({field.column_name})"
                )
        return sqls

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
        raise ValueError(
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
            raise ValueError(f"Table '{cls.table_name}' does not exist")

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
        """Insert or update this instance in the database."""
        pk_value = self.__dict__.get(self._pk_name)
        for name, field in self._fields.items():
            value = self.__dict__.get(name)
            field.validate(value)
        if self._persisted and pk_value is not None:
            self._update()
        else:
            self._insert()

    async def asave(self) -> None:
        """Async version of :meth:`save`."""
        await asyncio.to_thread(self.save)

    def _insert(self) -> None:
        non_pk_fields = {name: field for name, field in self._fields.items() if not field.primary_key}
        columns = [f.column_name for f in non_pk_fields.values()]
        values = [f.to_db(self.__dict__.get(name)) for name, f in non_pk_fields.items()]
        placeholders = ", ".join("?" for _ in columns)
        sql = (
            f"INSERT INTO {self.table_name} ({', '.join(columns)}) "
            f"VALUES ({placeholders})"
        )
        cursor = Database.execute(sql, values)
        self.__dict__[self._pk_name] = cursor.lastrowid
        self._persisted = True

    def _update(self) -> None:
        non_pk_fields = {name: field for name, field in self._fields.items() if not field.primary_key}
        set_clause = ", ".join(f"{f.column_name} = ?" for f in non_pk_fields.values())
        values = [f.to_db(self.__dict__.get(name)) for name, f in non_pk_fields.items()]
        pk_value = self.__dict__[self._pk_name]
        values.append(pk_value)
        pk_field = type(self)._pk_field
        sql = (
            f"UPDATE {self.table_name} SET {set_clause} "
            f"WHERE {pk_field.column_name} = ?"
        )
        Database.execute(sql, values)

    def delete(self) -> None:
        """Delete this instance from the database."""
        pk_value = self.__dict__.get(self._pk_name)
        if pk_value is None:
            raise RecordNotFoundError("Cannot delete an unsaved instance")
        pk_field = type(self)._pk_field
        sql = f"DELETE FROM {self.table_name} WHERE {pk_field.column_name} = ?"
        Database.execute(sql, [pk_value])
        self.__dict__[self._pk_name] = None
        self._persisted = False

    async def adelete(self) -> None:
        """Async version of :meth:`delete`."""
        await asyncio.to_thread(self.delete)

    def refresh(self) -> None:
        """Re-read this instance's data from the database."""
        pk_value = self.__dict__.get(self._pk_name)
        if pk_value is None:
            raise RecordNotFoundError("Cannot refresh an unsaved instance")
        pk_field = type(self)._pk_field
        sql = f"SELECT * FROM {self.table_name} WHERE {pk_field.column_name} = ?"
        row = Database.fetchone(sql, [pk_value])
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
    def _from_row(
        cls,
        row_dict: dict[str, Any],
        *,
        annotations: dict[str, Any] | None = None,
    ) -> Model:
        """Construct a model instance from a database row dict."""
        kwargs: dict[str, Any] = {}
        for name, field in cls._fields.items():
            col = field.column_name
            if col in row_dict:
                raw = row_dict[col]
                kwargs[name] = field.to_python(raw) if raw is not None else None
        instance = cls.__new__(cls)
        Model.__init__(instance, **kwargs, _persisted=True)
        instance._annotations = annotations or {}
        for alias, value in instance._annotations.items():
            setattr(instance, alias, value)
        return instance

    # ---- Create / get_or_create -------------------------------------------

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
        try:
            instance = cls.get(**kwargs)
            return instance, False
        except RecordNotFoundError:
            if defaults:
                kwargs.update(defaults)
            instance = cls.create(**kwargs)
            return instance, True

    @classmethod
    async def aget_or_create(cls, defaults: dict[str, Any] | None = None, **kwargs: Any) -> tuple[Model, bool]:
        """Async version of :meth:`get_or_create`."""
        return await asyncio.to_thread(cls.get_or_create, defaults, **kwargs)

    # ---- Bulk operations --------------------------------------------------

    @classmethod
    def bulk_create(
        cls,
        items: list[dict[str, Any]],
        *,
        validate: bool = True,
    ) -> list[Model]:
        """Insert many rows efficiently and return the instances.

        When *validate* is ``True`` (default), each value is validated
        against its field constraints before insertion.  Set to ``False``
        to skip validation for maximum throughput with trusted data.
        """
        if not items:
            return []

        non_pk_fields = {
            name: field
            for name, field in cls._fields.items()
            if not field.primary_key
        }
        columns = [f.column_name for f in non_pk_fields.values()]
        placeholders = ", ".join("?" for _ in columns)
        sql = (
            f"INSERT INTO {cls.table_name} ({', '.join(columns)}) "
            f"VALUES ({placeholders})"
        )

        errors: list[str] = []
        params_seq = []
        for idx, item in enumerate(items):
            row_values = []
            for name, field in non_pk_fields.items():
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

        Database.executemany(sql, params_seq)

        # Fetch the inserted rows (SQLite doesn't return multiple lastrowids)
        fetch_sql = (
            f"SELECT * FROM {cls.table_name} ORDER BY rowid DESC LIMIT {len(items)}"
        )
        rows = Database.fetchall(fetch_sql)
        return [cls._from_row(dict(r)) for r in reversed(rows)]

    @classmethod
    async def abulk_create(
        cls,
        items: list[dict[str, Any]],
        *,
        validate: bool = True,
    ) -> list[Model]:
        """Async version of :meth:`bulk_create`."""
        return await asyncio.to_thread(cls.bulk_create, items, validate=validate)

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

