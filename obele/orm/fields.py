"""Typed field descriptors for the ORM.

Each field maps a Python type to a SQLite column type, with support for
validation, serialization, and column-level constraints.
"""

from __future__ import annotations

import datetime
import json
from typing import Any, TYPE_CHECKING

from .exceptions import FieldValidationError

if TYPE_CHECKING:
    from .model import Model


_MISSING = object()


class Field:
    """Base descriptor for all ORM fields.

    Subclasses must set ``sql_type`` and ``python_type``.
    """

    sql_type: str = ""
    python_type: type = object

    __slots__ = (
        "primary_key", "nullable", "default", "db_default",
        "unique", "index", "column_name", "attr_name", "_cached_ddl",
    )

    def __init__(
        self,
        *,
        primary_key: bool = False,
        nullable: bool = False,
        default: Any = _MISSING,
        db_default: str | None = None,
        unique: bool = False,
        index: bool = False,
        column_name: str = "",
    ):
        self.primary_key = primary_key
        self.nullable = nullable
        self.default = default
        self.db_default = db_default
        self.unique = unique
        self.index = index
        self.column_name = column_name
        self.attr_name = ""
        self._cached_ddl: str | None = None

    def __set_name__(self, owner: type, name: str) -> None:
        self.attr_name = name
        if not self.column_name:
            self.column_name = name
        self._cached_ddl = None  # Invalidate any prior cache

    def __get__(self, instance: Model | None, owner: type) -> Any:
        if instance is None:
            return self
        return instance.__dict__.get(
            self.attr_name,
            self.default if self.default is not _MISSING else None,
        )

    def __set__(self, instance: Model, value: Any) -> None:
        if value is None:
            if not self.nullable and not self.primary_key:
                raise FieldValidationError(f"Field '{self.column_name}' does not allow None")
            instance.__dict__[self.attr_name] = None
            return
        instance.__dict__[self.attr_name] = self.to_python(value)

    def to_python(self, value: Any) -> Any:
        """Convert *value* coming from the database (or user) to the Python type."""
        if isinstance(value, self.python_type):
            return value
        try:
            return self.python_type(value)
        except (TypeError, ValueError) as exc:
            raise FieldValidationError(
                f"Cannot convert {value!r} to {self.python_type.__name__} "
                f"for field '{self.column_name}'"
            ) from exc

    def to_db(self, value: Any) -> Any:
        """Convert a Python value to something SQLite can store."""
        return None if value is None else value

    def validate(self, value: Any) -> None:
        """Validate *value* against this field's constraints.

        Called automatically before writing to the database.
        """
        if value is None:
            if not self.nullable and not self.primary_key:
                raise FieldValidationError(f"Field '{self.column_name}' does not allow None")
            return
        if not isinstance(value, self.python_type):
            raise FieldValidationError(
                f"Expected {self.python_type.__name__} for field "
                f"'{self.column_name}', got {type(value).__name__}"
            )

    def column_ddl(self) -> str:
        """Return the full column-definition fragment for CREATE TABLE."""
        if self._cached_ddl is not None:
            return self._cached_ddl
        parts = [self.column_name, self.sql_type]
        if self.primary_key:
            parts.append("PRIMARY KEY")
            if isinstance(self, IntegerField) and not isinstance(self, BooleanField):
                parts.append("AUTOINCREMENT")
        if not self.nullable and not self.primary_key:
            parts.append("NOT NULL")
        if self.unique and not self.primary_key:
            parts.append("UNIQUE")
        if self.db_default is not None:
            parts.append(f"DEFAULT {self.db_default}")
        elif self.default is not _MISSING and self.default is not None and not callable(self.default):
            db_val = self.to_db(self.default)
            if isinstance(db_val, str):
                parts.append(f"DEFAULT '{db_val}'")
            else:
                parts.append(f"DEFAULT {db_val}")
        ddl = " ".join(parts)
        self._cached_ddl = ddl
        return ddl

    def __repr__(self) -> str:
        return f"<{type(self).__name__} column={self.column_name!r}>"


class IntegerField(Field):
    sql_type = "INTEGER"
    python_type = int


class TextField(Field):
    sql_type = "TEXT"
    python_type = str

    __slots__ = ("max_length",)

    def __init__(self, *, max_length: int | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.max_length = max_length

    def validate(self, value: Any) -> None:
        super().validate(value)
        if value is not None and self.max_length and len(value) > self.max_length:
            raise FieldValidationError(
                f"Field '{self.column_name}' exceeds max_length "
                f"({len(value)} > {self.max_length})"
            )


class RealField(Field):
    sql_type = "REAL"
    python_type = float

    def to_python(self, value: Any) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        return super().to_python(value)


class BlobField(Field):
    sql_type = "BLOB"
    python_type = bytes


class BooleanField(Field):
    """Stored as 0/1 in SQLite, exposed as ``bool`` in Python."""

    sql_type = "INTEGER"
    python_type = bool

    def to_python(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, int):
            return bool(value)
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes")
        return bool(value)

    def to_db(self, value: Any) -> int | None:
        return None if value is None else (1 if value else 0)


class DateTimeField(Field):
    """Stored as ISO-8601 TEXT in SQLite, exposed as ``datetime.datetime``."""

    sql_type = "TEXT"
    python_type = datetime.datetime

    def to_python(self, value: Any) -> datetime.datetime:
        if isinstance(value, datetime.datetime):
            return value
        if isinstance(value, str):
            return datetime.datetime.fromisoformat(value)
        raise FieldValidationError(
            f"Cannot convert {value!r} to datetime for field '{self.column_name}'"
        )

    def to_db(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime.datetime):
            return value.isoformat()
        return str(value)


class DateField(Field):
    """Stored as ISO-8601 TEXT (date only) in SQLite, exposed as ``datetime.date``."""

    sql_type = "TEXT"
    python_type = datetime.date

    def to_python(self, value: Any) -> datetime.date:
        if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
            return value
        if isinstance(value, datetime.datetime):
            return value.date()
        if isinstance(value, str):
            return datetime.date.fromisoformat(value)
        raise FieldValidationError(
            f"Cannot convert {value!r} to date for field '{self.column_name}'"
        )

    def to_db(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime.date):
            return value.isoformat()
        return str(value)


class TimestampField(Field):
    """Stored as INTEGER (Unix epoch seconds) in SQLite, exposed as ``datetime.datetime``.

    Faster for filtering and sorting than ISO-8601 text, and uses less storage.
    """

    sql_type = "INTEGER"
    python_type = datetime.datetime

    def to_python(self, value: Any) -> datetime.datetime:
        if isinstance(value, datetime.datetime):
            return value
        if isinstance(value, (int, float)):
            return datetime.datetime.fromtimestamp(value, tz=datetime.timezone.utc)
        if isinstance(value, str):
            return datetime.datetime.fromisoformat(value)
        raise FieldValidationError(
            f"Cannot convert {value!r} to datetime for field '{self.column_name}'"
        )

    def to_db(self, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, datetime.datetime):
            return int(value.timestamp())
        if isinstance(value, (int, float)):
            return int(value)
        raise FieldValidationError(
            f"Cannot convert {value!r} to timestamp for field '{self.column_name}'"
        )

    def validate(self, value: Any) -> None:
        if value is None:
            if not self.nullable and not self.primary_key:
                raise FieldValidationError(f"Field '{self.column_name}' does not allow None")
            return
        if not isinstance(value, (datetime.datetime, int, float)):
            raise FieldValidationError(
                f"Expected datetime or numeric for field "
                f"'{self.column_name}', got {type(value).__name__}"
            )


class JSONField(Field):
    """Stored as TEXT (JSON) in SQLite, exposed as Python dict/list/scalar.

    Supports any JSON-serializable Python value::

        class Config(Model):
            data = JSONField(default=dict)

        config = Config.create(data={"theme": "dark", "lang": "en"})
    """

    sql_type = "TEXT"
    python_type = object  # Accept any JSON-serializable type

    def to_python(self, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise FieldValidationError(
                    f"Invalid JSON for field '{self.column_name}': {exc}"
                ) from exc
        return value  # Already a Python object

    def to_db(self, value: Any) -> str | None:
        if value is None:
            return None
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise FieldValidationError(
                f"Cannot serialize value to JSON for field '{self.column_name}'"
            ) from exc

    def validate(self, value: Any) -> None:
        if value is None:
            if not self.nullable and not self.primary_key:
                raise FieldValidationError(f"Field '{self.column_name}' does not allow None")
            return
        # Validate it's JSON-serializable
        try:
            json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise FieldValidationError(
                f"Value for field '{self.column_name}' is not JSON-serializable"
            ) from exc


class ForeignKeyField(Field):
    """Integer foreign key referencing another Model's primary key.

    Accepts either an integer PK or a model instance::

        class Post(Model):
            author = ForeignKeyField(to=User)

        post.author = user_instance  # stores PK, caches instance
        post.author = 3              # stores raw PK
    """

    sql_type = "INTEGER"
    python_type = int

    __slots__ = ("to", "on_delete", "related_name", "_related_model")

    def __init__(
        self,
        *,
        to: type[Model] | str,
        on_delete: str = "CASCADE",
        related_name: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.to = to  # resolved lazily if passed as string
        self.on_delete = on_delete.upper()
        self.related_name = related_name
        self._related_model: type[Model] | None = None

    @property
    def cache_attr_name(self) -> str:
        """Name of the dict key used to cache the hydrated related object."""
        return f"_{self.attr_name}_related_cache"

    def __get__(self, instance: Model | None, owner: type) -> Any:
        if instance is None:
            return self
        # Return cached related instance if available
        cached = instance.__dict__.get(self.cache_attr_name)
        if cached is not None:
            return cached
        return instance.__dict__.get(
            self.attr_name,
            self.default if self.default is not _MISSING else None,
        )

    def __set__(self, instance: Model, value: Any) -> None:
        if value is None:
            if not self.nullable and not self.primary_key:
                raise FieldValidationError(f"Field '{self.column_name}' does not allow None")
            instance.__dict__.pop(self.cache_attr_name, None)
            instance.__dict__[self.attr_name] = None
            return

        # Accept a model instance - extract its PK and cache the object
        related = self.related_model
        if isinstance(value, related):
            pk_value = value.__dict__.get(value._pk_name)
            if pk_value is None:
                raise FieldValidationError(
                    f"Cannot assign unsaved {related.__name__} to '{self.column_name}'"
                )
            instance.__dict__[self.attr_name] = pk_value
            instance.__dict__[self.cache_attr_name] = value
            return

        # Raw integer PK
        instance.__dict__.pop(self.cache_attr_name, None)
        instance.__dict__[self.attr_name] = self.to_python(value)

    @property
    def related_model(self) -> type[Model]:
        if self._related_model is not None:
            return self._related_model
        if isinstance(self.to, str):
            from .model import _model_registry
            model = _model_registry.get(self.to)
            if model is None:
                raise FieldValidationError(f"Model '{self.to}' not found in registry")
            self._related_model = model
            return model
        self._related_model = self.to
        return self.to

    def column_ddl(self) -> str:
        if self._cached_ddl is not None:
            return self._cached_ddl
        base = super().column_ddl()
        # Reset base cache since we extend it
        self._cached_ddl = None
        ref_table = self.related_model.table_name
        pk = self.related_model._pk_field.column_name
        ddl = f"{base} REFERENCES {ref_table}({pk}) ON DELETE {self.on_delete}"
        self._cached_ddl = ddl
        return ddl

    def validate(self, value: Any) -> None:
        if value is None:
            return super().validate(value)
        related = self.related_model
        if isinstance(value, related):
            pk_value = value.__dict__.get(value._pk_name)
            if pk_value is None:
                raise FieldValidationError(
                    f"Cannot assign unsaved {related.__name__} to '{self.column_name}'"
                )
            return
        super().validate(value)

    def to_db(self, value: Any) -> Any:
        if value is None:
            return None
        related = self.related_model
        if isinstance(value, related):
            pk_value = value.__dict__.get(value._pk_name)
            if pk_value is None:
                raise FieldValidationError(
                    f"Cannot assign unsaved {related.__name__} to '{self.column_name}'"
                )
            return pk_value
        return super().to_db(value)
