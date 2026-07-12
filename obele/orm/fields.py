"""Typed field descriptors for the ORM.

Each field maps a Python type to a SQLite column type, with support for
validation, serialization, and column-level constraints.
"""

from __future__ import annotations

import datetime
import decimal
import enum
import ipaddress
import json
import pickle
import re
import uuid
from collections.abc import Callable, Sequence
from typing import Any, TYPE_CHECKING

from .exceptions import FieldValidationError
from .sql import validate_identifier

if TYPE_CHECKING:
    from .model import Model


_MISSING = object()

_ON_DELETE_ACTIONS = frozenset({"CASCADE", "SET NULL", "SET DEFAULT", "RESTRICT", "NO ACTION"})


def _sql_literal(value: Any) -> str:
    """Render a simple SQLite literal for DDL defaults."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    return "'" + str(value).replace("'", "''") + "'"


class Field:
    """Base descriptor for all ORM fields.

    Args:
        primary_key: Mark this column as the table's primary key.
        nullable: Allow ``None`` values.
        default: Python-side default (a value or a zero-arg callable).
        db_default: Raw SQL expression used as the column ``DEFAULT``.
        unique: Add a UNIQUE constraint.
        index: Create a secondary index on this column.
        column_name: Override the column name (defaults to the attribute name).
        validators: Extra callables ``(value) -> None`` that raise on bad input.
        check: Raw SQL ``CHECK`` expression for the column.
        choices: Iterable of permitted values.
    """

    sql_type: str = ""
    python_type: type = object

    __slots__ = (
        "primary_key", "nullable", "default", "db_default", "unique", "index",
        "column_name", "attr_name", "validators", "check", "choices", "_cached_ddl",
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
        validators: Sequence[Callable[[Any], None]] | None = None,
        check: str | None = None,
        choices: Sequence[Any] | None = None,
    ) -> None:
        self.primary_key = primary_key
        self.nullable = nullable
        self.default = default
        self.db_default = db_default
        self.unique = unique
        self.index = index
        self.column_name = column_name
        self.validators = tuple(validators or ())
        self.check = check
        self.choices = tuple(choices) if choices is not None else None
        self.attr_name = ""
        self._cached_ddl: str | None = None

    def __set_name__(self, owner: type, name: str) -> None:
        self.attr_name = name
        if not self.column_name:
            self.column_name = name
        validate_identifier(self.column_name, kind="column name")
        self._cached_ddl = None

    def __get__(self, instance: Model | None, owner: type) -> Any:
        if instance is None:
            return self
        return instance.__dict__.get(self.attr_name)

    def __set__(self, instance: Model, value: Any) -> None:
        if value is None:
            self._reject_null()
            instance.__dict__[self.attr_name] = None
            return
        instance.__dict__[self.attr_name] = self.to_python(value)

    def _reject_null(self) -> None:
        if not self.nullable and not self.primary_key:
            raise FieldValidationError(f"Field '{self.column_name}' does not allow None")

    def to_python(self, value: Any) -> Any:
        """Convert *value* coming from the database (or user) to the Python type."""
        if isinstance(value, self.python_type):
            return value
        try:
            return self.python_type(value)
        except (TypeError, ValueError) as exc:
            raise FieldValidationError(
                f"Cannot convert {value!r} to {self.python_type.__name__} for field '{self.column_name}'"
            ) from exc

    def to_db(self, value: Any) -> Any:
        """Convert a Python value to something SQLite can store."""
        return value

    def validate(self, value: Any) -> None:
        """Validate *value* against this field's constraints (pre-write)."""
        if value is None:
            self._reject_null()
            return
        if not isinstance(value, self.python_type):
            raise FieldValidationError(
                f"Expected {self.python_type.__name__} for field "
                f"'{self.column_name}', got {type(value).__name__}"
            )
        self._run_extra_validation(value)

    def _run_extra_validation(self, value: Any) -> None:
        if self.choices is not None and value not in self.choices:
            raise FieldValidationError(
                f"Field '{self.column_name}' must be one of {self.choices!r}, got {value!r}"
            )
        for validator in self.validators:
            validator(value)

    def column_ddl(self) -> str:
        """Return the column-definition fragment for CREATE TABLE."""
        if self._cached_ddl is not None:
            return self._cached_ddl
        parts = [self.column_name, self.sql_type]
        if self.primary_key:
            parts.append("PRIMARY KEY")
        if not self.nullable and not self.primary_key:
            parts.append("NOT NULL")
        if self.unique and not self.primary_key:
            parts.append("UNIQUE")
        if self.db_default is not None:
            parts.append(f"DEFAULT {self.db_default}")
        elif self.default is not _MISSING and self.default is not None and not callable(self.default):
            parts.append(f"DEFAULT {_sql_literal(self.to_db(self.default))}")
        if self.check is not None:
            parts.append(f"CHECK ({self.check})")
        self._cached_ddl = " ".join(parts)
        return self._cached_ddl

    def __repr__(self) -> str:
        return f"<{type(self).__name__} column={self.column_name!r}>"


class _BoundedNumericMixin:
    """Adds ``min_value`` / ``max_value`` range validation."""

    __slots__ = ()

    def _init_bounds(self, min_value: Any, max_value: Any) -> None:
        self.min_value = min_value
        self.max_value = max_value

    def _check_bounds(self, value: Any) -> None:
        if self.min_value is not None and value < self.min_value:
            raise FieldValidationError(
                f"Field '{self.column_name}' must be >= {self.min_value}, got {value!r}"
            )
        if self.max_value is not None and value > self.max_value:
            raise FieldValidationError(
                f"Field '{self.column_name}' must be <= {self.max_value}, got {value!r}"
            )


class IntegerField(_BoundedNumericMixin, Field):
    sql_type = "INTEGER"
    python_type = int

    __slots__ = ("min_value", "max_value")

    def __init__(self, *, min_value: int | None = None, max_value: int | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self._init_bounds(min_value, max_value)

    def validate(self, value: Any) -> None:
        super().validate(value)
        if value is not None:
            self._check_bounds(value)


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


class RealField(_BoundedNumericMixin, Field):
    sql_type = "REAL"
    python_type = float

    __slots__ = ("min_value", "max_value")

    def __init__(self, *, min_value: float | None = None, max_value: float | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self._init_bounds(min_value, max_value)

    def to_python(self, value: Any) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        return super().to_python(value)

    def validate(self, value: Any) -> None:
        if value is None:
            return super().validate(value)
        if not isinstance(value, (int, float)):
            raise FieldValidationError(
                f"Expected float for field '{self.column_name}', got {type(value).__name__}"
            )
        self._check_bounds(value)
        for validator in self.validators:
            validator(float(value))


class BlobField(Field):
    sql_type = "BLOB"
    python_type = bytes


class DecimalField(Field):
    """Stored as TEXT, exposed as :class:`decimal.Decimal`."""

    sql_type = "TEXT"
    python_type = decimal.Decimal

    def to_python(self, value: Any) -> decimal.Decimal:
        if isinstance(value, decimal.Decimal):
            return value
        try:
            return decimal.Decimal(str(value))
        except decimal.InvalidOperation as exc:
            raise FieldValidationError(
                f"Cannot convert {value!r} to Decimal for field '{self.column_name}'"
            ) from exc

    def to_db(self, value: Any) -> str | None:
        return None if value is None else str(self.to_python(value))

    def validate(self, value: Any) -> None:
        if value is None:
            return super().validate(value)
        converted = self.to_python(value)
        self._run_extra_validation(converted)


class UUIDField(Field):
    """Stored as TEXT, exposed as :class:`uuid.UUID`."""

    sql_type = "TEXT"
    python_type = uuid.UUID

    def to_python(self, value: Any) -> uuid.UUID:
        if isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(str(value))
        except (TypeError, ValueError) as exc:
            raise FieldValidationError(
                f"Cannot convert {value!r} to UUID for field '{self.column_name}'"
            ) from exc

    def to_db(self, value: Any) -> str | None:
        return None if value is None else str(self.to_python(value))

    def validate(self, value: Any) -> None:
        if value is None:
            return super().validate(value)
        converted = self.to_python(value)
        self._run_extra_validation(converted)


class BooleanField(Field):
    """Stored as 0/1 in SQLite, exposed as ``bool`` in Python."""

    sql_type = "INTEGER"
    python_type = bool

    def to_python(self, value: Any) -> bool:
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes")
        return bool(value)

    def to_db(self, value: Any) -> int | None:
        return None if value is None else int(bool(value))


class DateTimeField(Field):
    """Stored as ISO-8601 TEXT, exposed as ``datetime.datetime``.

    Pass ``default=datetime.datetime.now`` (or any callable) for an
    auto-populated timestamp; the field itself applies no implicit default.
    """

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
    """Stored as ISO-8601 TEXT (date only), exposed as ``datetime.date``."""

    sql_type = "TEXT"
    python_type = datetime.date

    def to_python(self, value: Any) -> datetime.date:
        if isinstance(value, datetime.datetime):
            return value.date()
        if isinstance(value, datetime.date):
            return value
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


class TimeField(Field):
    """Stored as ISO-8601 TEXT (time only), exposed as ``datetime.time``."""

    sql_type = "TEXT"
    python_type = datetime.time

    def to_python(self, value: Any) -> datetime.time:
        if isinstance(value, datetime.time):
            return value
        if isinstance(value, str):
            return datetime.time.fromisoformat(value)
        raise FieldValidationError(
            f"Cannot convert {value!r} to time for field '{self.column_name}'"
        )

    def to_db(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime.time):
            return value.isoformat()
        return str(value)


class TimestampField(Field):
    """Stored as INTEGER Unix-epoch seconds, exposed as UTC ``datetime.datetime``.

    Faster to filter and sort than ISO-8601 text, and uses less storage.
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
            return self._reject_null()
        if not isinstance(value, (datetime.datetime, int, float)):
            raise FieldValidationError(
                f"Expected datetime or numeric for field "
                f"'{self.column_name}', got {type(value).__name__}"
            )
        self._run_extra_validation(value)


class JSONField(Field):
    """Stored as TEXT (JSON), exposed as Python dict/list/scalar::

        class Config(Model):
            data = JSONField(default=dict)
    """

    sql_type = "TEXT"
    python_type = object  # any JSON-serializable value

    def to_python(self, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise FieldValidationError(
                    f"Invalid JSON for field '{self.column_name}': {exc}"
                ) from exc
        return value

    def to_db(self, value: Any) -> str | None:
        if value is None:
            return None
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise FieldValidationError(
                f"Value for field '{self.column_name}' is not JSON-serializable"
            ) from exc

    def validate(self, value: Any) -> None:
        if value is None:
            return self._reject_null()
        self._run_extra_validation(value)


class ForeignKeyField(Field):
    """Integer foreign key referencing another Model's primary key.

    Accepts either a raw PK value or a saved model instance::

        class Post(Model):
            author = ForeignKeyField(to=User, related_name="posts")

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
    ) -> None:
        super().__init__(**kwargs)
        self.to = to  # resolved lazily when passed as a string
        self.on_delete = on_delete.upper()
        if self.on_delete not in _ON_DELETE_ACTIONS:
            raise ValueError(
                f"on_delete must be one of {sorted(_ON_DELETE_ACTIONS)}, got {on_delete!r}"
            )
        self.related_name = related_name
        self._related_model: type[Model] | None = None

    @property
    def cache_attr_name(self) -> str:
        """Dict key used to cache the hydrated related instance."""
        return f"_{self.attr_name}_related_cache"

    def __get__(self, instance: Model | None, owner: type) -> Any:
        if instance is None:
            return self
        cached = instance.__dict__.get(self.cache_attr_name)
        if cached is not None:
            return cached
        return instance.__dict__.get(self.attr_name)

    def __set__(self, instance: Model, value: Any) -> None:
        if value is None:
            self._reject_null()
            instance.__dict__.pop(self.cache_attr_name, None)
            instance.__dict__[self.attr_name] = None
            return
        related = self.related_model
        if isinstance(value, related):
            instance.__dict__[self.attr_name] = self._instance_pk(value)
            instance.__dict__[self.cache_attr_name] = value
            return
        instance.__dict__.pop(self.cache_attr_name, None)
        instance.__dict__[self.attr_name] = self.to_python(value)

    def _instance_pk(self, value: Model) -> Any:
        pk_value = value.__dict__.get(value._pk_name)
        if pk_value is None:
            raise FieldValidationError(
                f"Cannot assign unsaved {type(value).__name__} to '{self.column_name}'"
            )
        return pk_value

    @property
    def related_model(self) -> type[Model]:
        if self._related_model is None:
            if isinstance(self.to, str):
                from .model import _model_registry
                model = _model_registry.get(self.to)
                if model is None:
                    raise FieldValidationError(f"Model '{self.to}' not found in registry")
                self._related_model = model
            else:
                self._related_model = self.to
        return self._related_model

    def column_ddl(self) -> str:
        if self._cached_ddl is None:
            base = super().column_ddl()
            self._cached_ddl = None  # base cached itself; rebuild with the reference
            related = self.related_model
            self._cached_ddl = (
                f"{base} REFERENCES {related.table_name}({related._pk_field.column_name}) "
                f"ON DELETE {self.on_delete}"
            )
        return self._cached_ddl

    def validate(self, value: Any) -> None:
        if value is None:
            return self._reject_null()
        if isinstance(value, self.related_model):
            self._instance_pk(value)
            return
        super().validate(value)

    def to_db(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, self.related_model):
            return self._instance_pk(value)
        return value


class EnumField(Field):
    """Stored as TEXT, exposed as a Python :class:`~enum.Enum`::

        class Status(enum.Enum):
            DRAFT = "draft"
            PUBLISHED = "published"

        class Post(Model):
            status = EnumField(enum_class=Status, default=Status.DRAFT)
    """

    sql_type = "TEXT"
    python_type = str  # storage type

    __slots__ = ("enum_class",)

    def __init__(self, *, enum_class: type[enum.Enum], **kwargs: Any):
        super().__init__(**kwargs)
        self.enum_class = enum_class

    def to_python(self, value: Any) -> enum.Enum:
        if isinstance(value, self.enum_class):
            return value
        for candidate in (value, str(value)):
            try:
                return self.enum_class(candidate)
            except (ValueError, TypeError):
                pass
        if isinstance(value, str):
            try:
                return self.enum_class[value]
            except KeyError:
                pass
            try:
                return self.enum_class(int(value))
            except (ValueError, KeyError):
                pass
        raise FieldValidationError(
            f"Cannot convert {value!r} to {self.enum_class.__name__} "
            f"for field '{self.column_name}'"
        )

    def to_db(self, value: Any) -> Any:
        if value is None:
            return None
        return self.to_python(value).value

    def validate(self, value: Any) -> None:
        if value is None:
            return self._reject_null()
        self.to_python(value)
        for validator in self.validators:
            validator(value)

    def __set__(self, instance: Any, value: Any) -> None:
        if value is None:
            self._reject_null()
            instance.__dict__[self.attr_name] = None
            return
        instance.__dict__[self.attr_name] = self.to_python(value)


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SlugField(TextField):
    """A :class:`TextField` validating URL-safe slugs (lowercase, hyphenated)::

        class Article(Model):
            slug = SlugField(max_length=200, unique=True, index=True)
    """

    __slots__ = ()

    def __init__(self, *, max_length: int = 255, **kwargs: Any):
        super().__init__(max_length=max_length, **kwargs)

    def validate(self, value: Any) -> None:
        super().validate(value)
        if value is not None and not _SLUG_RE.match(str(value)):
            raise FieldValidationError(
                f"Field '{self.column_name}' must be a valid slug "
                f"(lowercase alphanumeric + hyphens), got {value!r}"
            )


_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)


class EmailField(TextField):
    """A :class:`TextField` that validates email address format::

        class User(Model):
            email = EmailField(unique=True, index=True)
    """

    __slots__ = ()

    def __init__(self, *, max_length: int = 254, **kwargs: Any):
        super().__init__(max_length=max_length, **kwargs)

    def validate(self, value: Any) -> None:
        super().validate(value)
        if value is not None and not _EMAIL_RE.match(str(value)):
            raise FieldValidationError(
                f"Field '{self.column_name}' must be a valid email address, got {value!r}"
            )


class PickleField(Field):
    """Stored as BLOB (pickled bytes), exposed as arbitrary Python objects::

        class Task(Model):
            metadata = PickleField(nullable=True)
    """

    sql_type = "BLOB"
    python_type = object

    def to_python(self, value: Any) -> Any:
        if isinstance(value, (bytes, bytearray, memoryview)):
            try:
                return pickle.loads(bytes(value))
            except Exception as exc:
                raise FieldValidationError(
                    f"Cannot unpickle value for field '{self.column_name}'"
                ) from exc
        return value

    def to_db(self, value: Any) -> bytes | None:
        if value is None:
            return None
        try:
            return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        except (pickle.PicklingError, TypeError) as exc:
            raise FieldValidationError(
                f"Cannot pickle value for field '{self.column_name}'"
            ) from exc

    def validate(self, value: Any) -> None:
        if value is None:
            return self._reject_null()
        self._run_extra_validation(value)


class IPAddressField(Field):
    """Stored as TEXT, exposed as :class:`~ipaddress.IPv4Address` /
    :class:`~ipaddress.IPv6Address`::

        class AccessLog(Model):
            client_ip = IPAddressField()
    """

    sql_type = "TEXT"
    python_type = object

    def to_python(self, value: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            return value
        try:
            return ipaddress.ip_address(str(value))
        except ValueError as exc:
            raise FieldValidationError(
                f"Cannot convert {value!r} to IP address for field '{self.column_name}'"
            ) from exc

    def to_db(self, value: Any) -> str | None:
        return None if value is None else str(self.to_python(value))

    def validate(self, value: Any) -> None:
        if value is None:
            return self._reject_null()
        self.to_python(value)
        self._run_extra_validation(value)
