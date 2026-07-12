"""Pagination utilities for QuerySet results.

Offset-based and cursor-based pagination::

    page = User.order_by("name").paginate(page=2, per_page=25)
    page.items, page.total, page.pages, page.has_next, page.has_prev

    page = User.order_by("id").cursor_paginate(per_page=25)
    nxt  = User.order_by("id").cursor_paginate(per_page=25, after=page.end_cursor)

Async versions live on the QuerySet (``apaginate`` / ``acursor_paginate``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Page:
    """Result of offset-based pagination."""

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
    """Result of cursor-based pagination."""

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
    """Offset-based pagination over *queryset* (1-indexed pages)."""
    if page < 1:
        raise ValueError("page must be >= 1")
    if per_page < 1:
        raise ValueError("per_page must be >= 1")

    total = queryset.count()
    pages = max(1, math.ceil(total / per_page))
    items = queryset.offset((page - 1) * per_page).limit(per_page).all()
    return Page(
        items=items, page=page, per_page=per_page, total=total, pages=pages,
        has_next=page < pages, has_prev=page > 1,
    )


def cursor_paginate_queryset(
    queryset: Any,
    *,
    per_page: int = 20,
    cursor_field: str = "",
    after: Any = None,
    before: Any = None,
) -> CursorPage:
    """Cursor-based pagination; efficient for large datasets.

    The queryset should be ordered by *cursor_field* (default: the PK).
    """
    if per_page < 1:
        raise ValueError("per_page must be >= 1")

    field = cursor_field or queryset.model_cls._pk_name
    if after is not None:
        queryset = queryset.filter(**{f"{field}__gt": after})
    elif before is not None:
        queryset = queryset.filter(**{f"{field}__lt": before})

    items = queryset.limit(per_page + 1).all()
    has_next = len(items) > per_page
    if has_next:
        items = items[:per_page]

    return CursorPage(
        items=items,
        per_page=per_page,
        has_next=has_next,
        has_prev=after is not None or before is not None,
        start_cursor=getattr(items[0], field, None) if items else None,
        end_cursor=getattr(items[-1], field, None) if items else None,
    )


__all__ = ["Page", "CursorPage", "paginate_queryset", "cursor_paginate_queryset"]
