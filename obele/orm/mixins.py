"""Reusable model mixins for common patterns.

Compose with :class:`~obele.orm.model.Model` via multiple inheritance::

    from obele import Model, TimestampMixin, SoftDeleteMixin

    class Article(TimestampMixin, SoftDeleteMixin, Model):
        title = TextField()

    article = Article.create(title="Hello")
    article.created_at   # auto-set on insert
    article.updated_at   # auto-set on every save

    article.delete()     # soft-delete (is_deleted=True, deleted_at=now)
    Article.all()        # excludes soft-deleted rows
    Article.with_deleted().all()
    Article.only_deleted().all()
    article.restore()
    article.hard_delete()  # permanent removal

The async methods (``asave``, ``adelete``, ...) inherited from ``Model``
automatically pick up these overrides - no async twins needed here.
"""

from __future__ import annotations

import datetime
from typing import Any, ClassVar

from .database import awrite
from .fields import DateTimeField, BooleanField
from .exceptions import RecordNotFoundError


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.timezone.utc)


class TimestampMixin:
    """Adds auto-managed ``created_at`` and ``updated_at`` fields."""

    created_at: ClassVar[DateTimeField] = DateTimeField(nullable=True, index=True)
    updated_at: ClassVar[DateTimeField] = DateTimeField(nullable=True, index=True)

    def save(self) -> None:
        now = _utcnow()
        if not getattr(self, "_persisted", False):
            self.__dict__["created_at"] = now
        self.__dict__["updated_at"] = now
        super().save()


class SoftDeleteMixin:
    """Soft-delete support via ``is_deleted`` / ``deleted_at``.

    :meth:`delete` marks the row deleted instead of removing it; default
    queries exclude soft-deleted rows.
    """

    is_deleted: ClassVar[BooleanField] = BooleanField(default=False, index=True)
    deleted_at: ClassVar[DateTimeField] = DateTimeField(nullable=True)

    def delete(self) -> None:
        """Soft-delete this instance (mark as deleted, keep the row)."""
        if self.pk is None:
            raise RecordNotFoundError("Cannot delete an unsaved instance")
        self.__dict__["is_deleted"] = True
        self.__dict__["deleted_at"] = _utcnow()
        self.save()

    def restore(self) -> None:
        """Un-delete a soft-deleted instance."""
        self.__dict__["is_deleted"] = False
        self.__dict__["deleted_at"] = None
        self.save()

    async def arestore(self) -> None:
        """Async version of :meth:`restore`."""
        await awrite(self.restore)

    def hard_delete(self) -> None:
        """Permanently remove this instance from the database."""
        from .model import Model
        Model.delete(self)

    async def ahard_delete(self) -> None:
        """Async version of :meth:`hard_delete`."""
        await awrite(self.hard_delete)

    @classmethod
    def _queryset(cls) -> Any:
        """Exclude soft-deleted rows by default."""
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
