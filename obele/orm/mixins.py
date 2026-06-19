"""Reusable model mixins for common patterns.

Provides :class:`TimestampMixin` and :class:`SoftDeleteMixin` that can be
composed with :class:`~obele.orm.model.Model` via multiple inheritance::

    from obele import Model, TimestampMixin, SoftDeleteMixin

    class Article(TimestampMixin, SoftDeleteMixin, Model):
        title = TextField()

    article = Article.create(title="Hello")
    article.created_at   # auto-set on insert
    article.updated_at   # auto-set on every save

    article.delete()     # soft-deletes (sets is_deleted=True, deleted_at=now)
    Article.all()        # excludes soft-deleted rows
    Article.with_deleted().all()   # includes soft-deleted
    Article.only_deleted().all()   # only soft-deleted

    article.restore()    # un-deletes
    article.hard_delete()  # permanent removal
"""

from __future__ import annotations

import datetime
from typing import Any, ClassVar

from .fields import DateTimeField, BooleanField
from .exceptions import RecordNotFoundError


class TimestampMixin:
    """Adds ``created_at`` and ``updated_at`` auto-managed fields.

    ``created_at`` is set once on first save.  ``updated_at`` is refreshed
    on every save.
    """

    created_at: ClassVar[DateTimeField] = DateTimeField(nullable=True, index=True,)
    updated_at: ClassVar[DateTimeField] = DateTimeField(nullable=True, index=True,)

    def save(self) -> None:
        """Override save to auto-set timestamps."""
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        if not getattr(self, "_persisted", False):
            self.__dict__["created_at"] = now
        self.__dict__["updated_at"] = now
        super().save()

    async def asave(self) -> None:
        """Async version of :meth:`save`."""
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        if not getattr(self, "_persisted", False):
            self.__dict__["created_at"] = now
        self.__dict__["updated_at"] = now
        await super().asave()


class SoftDeleteMixin:
    """Adds soft-delete support via ``is_deleted`` and ``deleted_at`` fields.

    Calling :meth:`delete` sets ``is_deleted=True`` and ``deleted_at=now``
    instead of removing the row.  Use :meth:`hard_delete` for permanent
    removal.  Default queries exclude soft-deleted rows.
    """

    is_deleted: ClassVar[BooleanField] = BooleanField(default=False, index=True,)
    deleted_at: ClassVar[DateTimeField] = DateTimeField(nullable=True,)

    def delete(self) -> None:
        """Soft-delete this instance (mark as deleted, keep the row)."""
        pk_value = self.__dict__.get(self._pk_name)
        if pk_value is None:
            raise RecordNotFoundError("Cannot delete an unsaved instance")
        self.__dict__["is_deleted"] = True
        self.__dict__["deleted_at"] = datetime.datetime.now(tz=datetime.timezone.utc)
        self.save()

    async def adelete(self) -> None:
        """Async version of :meth:`delete`."""
        pk_value = self.__dict__.get(self._pk_name)
        if pk_value is None:
            raise RecordNotFoundError("Cannot delete an unsaved instance")
        self.__dict__["is_deleted"] = True
        self.__dict__["deleted_at"] = datetime.datetime.now(tz=datetime.timezone.utc)
        await self.asave()

    def hard_delete(self) -> None:
        """Permanently remove this instance from the database."""
        from .model import Model
        # Call the real Model.delete (bypassing SoftDeleteMixin.delete)
        Model.delete(self)

    async def ahard_delete(self) -> None:
        """Async version of :meth:`hard_delete`."""
        from .model import Model
        await Model.adelete(self)

    def restore(self) -> None:
        """Un-delete a soft-deleted instance."""
        self.__dict__["is_deleted"] = False
        self.__dict__["deleted_at"] = None
        self.save()

    async def arestore(self) -> None:
        """Async version of :meth:`restore`."""
        self.__dict__["is_deleted"] = False
        self.__dict__["deleted_at"] = None
        await self.asave()

    @classmethod
    def _queryset(cls) -> Any:
        """Override to exclude soft-deleted rows by default."""
        from .query import QuerySet
        return QuerySet(cls).filter(is_deleted=False)

    @classmethod
    def with_deleted(cls) -> Any:
        """Return a QuerySet that includes soft-deleted rows."""
        from .query import QuerySet
        return QuerySet(cls)

    @classmethod
    def only_deleted(cls) -> Any:
        """Return a QuerySet of only soft-deleted rows."""
        from .query import QuerySet
        return QuerySet(cls).filter(is_deleted=True)


__all__ = ["TimestampMixin", "SoftDeleteMixin"]
