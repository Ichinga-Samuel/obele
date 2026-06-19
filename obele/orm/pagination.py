"""Pagination utilities for QuerySet results.

Provides :class:`Page` and :class:`CursorPage` for offset-based and
cursor-based pagination::

    # Offset-based
    page = User.order_by("name").paginate(page=2, per_page=25)
    page.items       # list[Model]
    page.total       # total matching rows
    page.pages       # total number of pages
    page.has_next    # bool
    page.has_prev    # bool

    # Cursor-based (efficient for large datasets)
    page = User.order_by("id").cursor_paginate(per_page=25)
    next_page = User.order_by("id").cursor_paginate(per_page=25, after=page.end_cursor)
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Model


@dataclass(frozen=True, slots=True)
class Page:
    """Result of offset-based pagination.

    Attributes
    ----------
    items:
        The rows for the current page.
    page:
        Current page number (1-indexed).
    per_page:
        Maximum items per page.
    total:
        Total number of matching rows across all pages.
    pages:
        Total number of pages.
    has_next:
        ``True`` if there is a page after this one.
    has_prev:
        ``True`` if there is a page before this one.
    """

    items: list[Any]
    page: int
    per_page: int
    total: int
    pages: int
    has_next: bool
    has_prev: bool

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __repr__(self) -> str:
        return f"<Page {self.page}/{self.pages} items={len(self.items)} total={self.total}>"


@dataclass(frozen=True, slots=True)
class CursorPage:
    """Result of cursor-based pagination.

    Attributes
    ----------
    items:
        The rows for the current page.
    per_page:
        Maximum items per page.
    has_next:
        ``True`` if there are more items after this page.
    has_prev:
        ``True`` if there are items before this page.
    start_cursor:
        Cursor pointing to the first item (use as ``before``).
    end_cursor:
        Cursor pointing to the last item (use as ``after``).
    """

    items: list[Any]
    per_page: int
    has_next: bool
    has_prev: bool
    start_cursor: Any
    end_cursor: Any

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __repr__(self) -> str:
        return f"<CursorPage items={len(self.items)} has_next={self.has_next} has_prev={self.has_prev}>"


def paginate_queryset(queryset: Any, *, page: int = 1, per_page: int = 20) -> Page:
    """Execute an offset-based pagination on *queryset*.

    Args:
        queryset: A :class:`~obele.orm.query.QuerySet` instance.
        page: Page number (1-indexed).
        per_page: Number of items per page.

    Returns:
        A :class:`Page` with results and metadata.
    """
    if page < 1:
        raise ValueError("page must be >= 1")
    if per_page < 1:
        raise ValueError("per_page must be >= 1")

    total = queryset.count()
    pages = max(1, math.ceil(total / per_page))
    offset = (page - 1) * per_page
    items = queryset.offset(offset).limit(per_page).all()

    return Page(items=items, page=page, per_page=per_page, total=total, pages=pages, has_next=page < pages, has_prev=page > 1)


async def apaginate_queryset(queryset: Any, *, page: int = 1, per_page: int = 20) -> Page:
    """Async version of :func:`paginate_queryset`."""
    return await asyncio.to_thread(paginate_queryset, queryset, page=page, per_page=per_page)


def cursor_paginate_queryset(queryset: Any, *, per_page: int = 20, cursor_field: str = "", after: Any = None,
                             before: Any = None,) -> CursorPage:
    """Execute cursor-based pagination on *queryset*.

    The queryset **must** have an ``order_by`` applied. The cursor field
    defaults to the model's primary key.

    Args:
        queryset: A :class:`~obele.orm.query.QuerySet` instance.
        per_page: Number of items per page.
        cursor_field: Field name to use as cursor. Defaults to PK.
        after: Fetch items after this cursor value.
        before: Fetch items before this cursor value.

    Returns:
        A :class:`CursorPage` with results and navigation cursors.
    """
    if per_page < 1:
        raise ValueError("per_page must be >= 1")

    field = cursor_field or queryset.model_cls._pk_name

    if after is not None:
        queryset = queryset.filter(**{f"{field}__gt": after})
    elif before is not None:
        queryset = queryset.filter(**{f"{field}__lt": before})

    # Fetch one extra to determine has_next
    items = queryset.limit(per_page + 1).all()
    has_next = len(items) > per_page
    if has_next:
        items = items[:per_page]

    has_prev = after is not None or before is not None
    start = getattr(items[0], field, None) if items else None
    end = getattr(items[-1], field, None) if items else None

    return CursorPage(items=items, per_page=per_page, has_next=has_next, has_prev=has_prev, start_cursor=start, end_cursor=end,)


async def acursor_paginate_queryset(queryset: Any, *, per_page: int = 20, cursor_field: str = "", after: Any = None,
                                    before: Any = None,) -> CursorPage:
    """Async version of :func:`cursor_paginate_queryset`."""
    return await asyncio.to_thread(
        cursor_paginate_queryset,
        queryset,
        per_page=per_page,
        cursor_field=cursor_field,
        after=after,
        before=before,
    )


__all__ = [
    "Page",
    "CursorPage",
    "paginate_queryset",
    "apaginate_queryset",
    "cursor_paginate_queryset",
    "acursor_paginate_queryset",
]
