"""Full-text search via SQLite FTS5.

Provides :class:`SearchIndex` for managing FTS5 virtual tables tied to
ORM models::

    from obele import Database, Model, TextField, SearchIndex

    Database.configure("app.sqlite3")

    class Article(Model):
        title = TextField()
        body  = TextField()

    Article.create_table()

    # Create an FTS5 index over title and body
    idx = SearchIndex(Article, fields=["title", "body"])
    idx.create()

    Article.create(title="Python Async", body="asyncio is great")
    Article.create(title="SQLite Tips", body="WAL mode is fast")

    idx.rebuild()  # Sync FTS with current table data

    results = idx.search("async")       # Ranked list of Article instances
    results = idx.search("sqlite tips") # FTS5 match syntax supported
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from .database import Database
from .sql import validate_identifier

if TYPE_CHECKING:
    from .model import Model


class SearchIndex:
    """FTS5 full-text search index tied to a :class:`Model`.

    Parameters
    ----------
    model_cls:
        The model class to index.
    fields:
        List of ``TextField`` attribute names to include in the index.
    fts_table:
        Custom FTS virtual table name.  Defaults to
        ``{model.table_name}_fts``.
    tokenizer:
        FTS5 tokenizer specification (e.g. ``"porter unicode61"``).
    content_sync:
        If ``True`` (default), creates a *content* FTS table that
        mirrors the source table. Set to ``False`` for an external
        content table that you manage manually.
    """

    __slots__ = (
        "model_cls", "fields", "fts_table", "tokenizer", "content_sync",
        "_field_columns",
    )

    def __init__(
        self,
        model_cls: type[Model],
        fields: list[str],
        *,
        fts_table: str | None = None,
        tokenizer: str = "unicode61",
        content_sync: bool = True,
    ) -> None:
        if not fields:
            raise ValueError("At least one field is required for a search index")
        self.model_cls = model_cls
        self.fields = fields
        self.fts_table = fts_table or f"{model_cls.table_name}_fts"
        validate_identifier(self.fts_table, kind="FTS table name")
        self.tokenizer = tokenizer
        self.content_sync = content_sync

        # Validate and resolve field → column mappings
        self._field_columns: list[str] = []
        for name in fields:
            field_obj = model_cls._fields.get(name)
            if field_obj is None:
                raise ValueError(
                    f"Unknown field {name!r} on {model_cls.__name__}"
                )
            self._field_columns.append(field_obj.column_name)

    # ---- DDL --------------------------------------------------------------

    def create(self) -> None:
        """Create the FTS5 virtual table."""
        columns = ", ".join(self._field_columns)
        source = self.model_cls.table_name
        pk_col = self.model_cls._pk_field.column_name

        if self.content_sync:
            sql = (
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.fts_table} "
                f"USING fts5({columns}, content={source!r}, "
                f"content_rowid={pk_col!r}, tokenize={self.tokenizer!r})"
            )
        else:
            sql = (
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.fts_table} "
                f"USING fts5({columns}, tokenize={self.tokenizer!r})"
            )
        Database.execute(sql)

        if self.content_sync:
            self._create_triggers()

    async def acreate(self) -> None:
        """Async version of :meth:`create`."""
        columns = ", ".join(self._field_columns)
        source = self.model_cls.table_name
        pk_col = self.model_cls._pk_field.column_name

        if self.content_sync:
            sql = (
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.fts_table} "
                f"USING fts5({columns}, content={source!r}, "
                f"content_rowid={pk_col!r}, tokenize={self.tokenizer!r})"
            )
        else:
            sql = (
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.fts_table} "
                f"USING fts5({columns}, tokenize={self.tokenizer!r})"
            )
        cursor = await Database.aexecute(sql)
        await cursor.close()

        if self.content_sync:
            await self._acreate_triggers()

    def drop(self) -> None:
        """Drop the FTS5 virtual table and associated triggers."""
        if self.content_sync:
            self._drop_triggers()
        Database.execute(f"DROP TABLE IF EXISTS {self.fts_table}")

    async def adrop(self) -> None:
        """Async version of :meth:`drop`."""
        if self.content_sync:
            await self._adrop_triggers()
        cursor = await Database.aexecute(f"DROP TABLE IF EXISTS {self.fts_table}")
        await cursor.close()

    def _create_triggers(self) -> None:
        """Create INSERT/UPDATE/DELETE triggers to keep FTS in sync."""
        source = self.model_cls.table_name
        pk_col = self.model_cls._pk_field.column_name
        fts = self.fts_table
        cols = ", ".join(self._field_columns)
        new_cols = ", ".join(f"new.{c}" for c in self._field_columns)
        old_cols = ", ".join(f"old.{c}" for c in self._field_columns)

        # After INSERT
        Database.execute(f"""
            CREATE TRIGGER IF NOT EXISTS {fts}_ai AFTER INSERT ON {source}
            BEGIN
                INSERT INTO {fts}(rowid, {cols}) VALUES (new.{pk_col}, {new_cols});
            END
        """)

        # After DELETE
        Database.execute(f"""
            CREATE TRIGGER IF NOT EXISTS {fts}_ad AFTER DELETE ON {source}
            BEGIN
                INSERT INTO {fts}({fts}, rowid, {cols})
                VALUES ('delete', old.{pk_col}, {old_cols});
            END
        """)

        # After UPDATE
        Database.execute(f"""
            CREATE TRIGGER IF NOT EXISTS {fts}_au AFTER UPDATE ON {source}
            BEGIN
                INSERT INTO {fts}({fts}, rowid, {cols})
                VALUES ('delete', old.{pk_col}, {old_cols});
                INSERT INTO {fts}(rowid, {cols}) VALUES (new.{pk_col}, {new_cols});
            END
        """)

    def _drop_triggers(self) -> None:
        """Remove the sync triggers."""
        fts = self.fts_table
        for suffix in ("ai", "ad", "au"):
            Database.execute(f"DROP TRIGGER IF EXISTS {fts}_{suffix}")

    async def _acreate_triggers(self) -> None:
        """Async version of :meth:`_create_triggers`."""
        source = self.model_cls.table_name
        pk_col = self.model_cls._pk_field.column_name
        fts = self.fts_table
        cols = ", ".join(self._field_columns)
        new_cols = ", ".join(f"new.{c}" for c in self._field_columns)
        old_cols = ", ".join(f"old.{c}" for c in self._field_columns)

        trigger_sqls = [
            f"""
            CREATE TRIGGER IF NOT EXISTS {fts}_ai AFTER INSERT ON {source}
            BEGIN
                INSERT INTO {fts}(rowid, {cols}) VALUES (new.{pk_col}, {new_cols});
            END
            """,
            f"""
            CREATE TRIGGER IF NOT EXISTS {fts}_ad AFTER DELETE ON {source}
            BEGIN
                INSERT INTO {fts}({fts}, rowid, {cols})
                VALUES ('delete', old.{pk_col}, {old_cols});
            END
            """,
            f"""
            CREATE TRIGGER IF NOT EXISTS {fts}_au AFTER UPDATE ON {source}
            BEGIN
                INSERT INTO {fts}({fts}, rowid, {cols})
                VALUES ('delete', old.{pk_col}, {old_cols});
                INSERT INTO {fts}(rowid, {cols}) VALUES (new.{pk_col}, {new_cols});
            END
            """,
        ]
        for sql in trigger_sqls:
            cursor = await Database.aexecute(sql)
            await cursor.close()

    async def _adrop_triggers(self) -> None:
        """Async version of :meth:`_drop_triggers`."""
        fts = self.fts_table
        for suffix in ("ai", "ad", "au"):
            cursor = await Database.aexecute(f"DROP TRIGGER IF EXISTS {fts}_{suffix}")
            await cursor.close()

    # ---- Data management --------------------------------------------------

    def rebuild(self) -> None:
        """Rebuild the FTS index from the source table data.

        Use after bulk inserts or when the FTS table is out of sync.
        """
        Database.execute(
            f"INSERT INTO {self.fts_table}({self.fts_table}) VALUES ('rebuild')"
        )

    async def arebuild(self) -> None:
        """Async version of :meth:`rebuild`."""
        cursor = await Database.aexecute(
            f"INSERT INTO {self.fts_table}({self.fts_table}) VALUES ('rebuild')"
        )
        await cursor.close()

    def optimize(self) -> None:
        """Run FTS5 merge optimization."""
        Database.execute(
            f"INSERT INTO {self.fts_table}({self.fts_table}) VALUES ('optimize')"
        )

    async def aoptimize(self) -> None:
        """Async version of :meth:`optimize`."""
        cursor = await Database.aexecute(
            f"INSERT INTO {self.fts_table}({self.fts_table}) VALUES ('optimize')"
        )
        await cursor.close()

    # ---- Search -----------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Model]:
        """Search the FTS index and return ranked model instances.

        Args:
            query: FTS5 match expression (e.g. ``"python async"``).
            limit: Maximum results to return.
            offset: Number of results to skip.

        Returns:
            List of model instances ordered by relevance (best first).
        """
        if not query or not query.strip():
            return []

        source = self.model_cls.table_name
        pk_col = self.model_cls._pk_field.column_name
        fts = self.fts_table

        sql = (
            f"SELECT {source}.* FROM {source} "
            f"INNER JOIN {fts} ON {source}.{pk_col} = {fts}.rowid "
            f"WHERE {fts} MATCH ? "
            f"ORDER BY {fts}.rank"
        )
        params: list[Any] = [query]

        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        if offset is not None:
            if limit is None:
                sql += " LIMIT -1"
            sql += " OFFSET ?"
            params.append(offset)

        rows = Database.fetchall(sql, params)
        return [self.model_cls._from_row(dict(row)) for row in rows]

    async def asearch(
        self,
        query: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Model]:
        """Async version of :meth:`search`."""
        if not query or not query.strip():
            return []

        source = self.model_cls.table_name
        pk_col = self.model_cls._pk_field.column_name
        fts = self.fts_table

        sql = (
            f"SELECT {source}.* FROM {source} "
            f"INNER JOIN {fts} ON {source}.{pk_col} = {fts}.rowid "
            f"WHERE {fts} MATCH ? "
            f"ORDER BY {fts}.rank"
        )
        params: list[Any] = [query]

        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        if offset is not None:
            if limit is None:
                sql += " LIMIT -1"
            sql += " OFFSET ?"
            params.append(offset)

        rows = await Database.afetchall(sql, params)
        return [self.model_cls._from_row(dict(row)) for row in rows]

    def search_count(self, query: str) -> int:
        """Return the number of rows matching the FTS query."""
        if not query or not query.strip():
            return 0
        fts = self.fts_table
        count = Database.fetch_value(
            f"SELECT COUNT(*) AS cnt FROM {fts} WHERE {fts} MATCH ?",
            [query],
            column="cnt",
        )
        return int(count or 0)

    async def asearch_count(self, query: str) -> int:
        """Async version of :meth:`search_count`."""
        if not query or not query.strip():
            return 0
        fts = self.fts_table
        count = await Database.afetch_value(
            f"SELECT COUNT(*) AS cnt FROM {fts} WHERE {fts} MATCH ?",
            [query],
            column="cnt",
        )
        return int(count or 0)

    def __repr__(self) -> str:
        return (
            f"<SearchIndex model={self.model_cls.__name__!r} "
            f"table={self.fts_table!r} fields={self.fields!r}>"
        )


__all__ = ["SearchIndex"]
