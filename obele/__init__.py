"""obele - A lightweight, async-ready ORM for SQLite.

Usage::

    from obele import Database, Model, TextField, IntegerField

    Database.configure("my_app.sqlite3")

    class User(Model):
        table_name = "users"
        name = TextField()
        age  = IntegerField(nullable=True)

    User.create_table()
    alice = User.create(name="Alice", age=30)
    users = User.filter(age__gte=18).order_by("name").all()
"""

from ._identity import PACKAGE_NAME
from .orm import (
    Database,
    Field,
    IntegerField,
    TextField,
    RealField,
    BlobField,
    BooleanField,
    DateTimeField,
    ForeignKeyField,
    Model,
    ReverseRelationManager,
    ReverseRelationDescriptor,
    QuerySet,
    Q,
    F,
    Value,
    RawSQL,
    Func,
    Count,
    Sum,
    Avg,
    Min,
    Max,
    Subquery,
    ORMError,
    FieldValidationError,
    RecordNotFoundError,
    MultipleResultsError,
    DatabaseError,
    IntegrityError,
)
from .kv import KVStore, KV

__title__ = PACKAGE_NAME

__all__ = [
    "Database",
    "Model",
    "ReverseRelationManager",
    "ReverseRelationDescriptor",
    "QuerySet",
    "Q",
    "F",
    "Value",
    "RawSQL",
    "Func",
    "Count",
    "Sum",
    "Avg",
    "Min",
    "Max",
    "Subquery",
    "Field",
    "IntegerField",
    "TextField",
    "RealField",
    "BlobField",
    "BooleanField",
    "DateTimeField",
    "ForeignKeyField",
    "ORMError",
    "FieldValidationError",
    "RecordNotFoundError",
    "MultipleResultsError",
    "DatabaseError",
    "IntegrityError",
    "KVStore",
    "KV",
]

