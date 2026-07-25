"""Full-text search via SQLite FTS5.

`SearchIndex` manages an FTS5 virtual table tied to an ORM model:

    from obele import Database, Model, TextField, SearchIndex

    class Article(Model):
        title = TextField()
        body  = TextField()

    Article.create_table()
    idx = SearchIndex(Article, fields=["title", "body"])
    idx.create()      # virtual table + sync triggers

    Article.create(title="Python Async", body="asyncio is great")
    results = idx.search("async")   # ranked Article instances
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any

from .database import Database, athread, awrite
from .sql import validate_identifier

if TYPE_CHECKING:
	from .model import Model


class SearchIndex:
	"""FTS5 full-text search index tied to a `Model`.

	Args:
	    model_cls: The model class to index.
	    fields: `TextField` attribute names to include in the index.
	    fts_table: Custom FTS table name (default `{table}_fts`).
	    tokenizer: FTS5 tokenizer spec (e.g. `"porter unicode61"`).
	    content_sync: When `True` (default), the FTS table mirrors the
	        source table and triggers keep it in sync automatically.
	"""

	__slots__ = ("model_cls", "fields", "fts_table", "tokenizer", "content_sync", "_field_columns")

	def __init__(
		self,
		model_cls: type[Model],
		fields: list[str],
		*,
		fts_table: str | None = None,
		tokenizer: str = "unicode61",
		content_sync: bool = True,
	) -> None:
		"""Validate the indexed fields, resolve their columns, and pick the FTS table name."""
		if not fields:
			raise ValueError("At least one field is required for a search index")
		self.model_cls = model_cls
		self.fields = fields
		self.fts_table = fts_table or f"{model_cls.table_name}_fts"
		validate_identifier(self.fts_table, kind="FTS table name")
		self.tokenizer = tokenizer
		self.content_sync = content_sync

		self._field_columns: list[str] = []
		for name in fields:
			field_obj = model_cls._fields.get(name)
			if field_obj is None:
				raise ValueError(f"Unknown field {name!r} on {model_cls.__name__}")
			self._field_columns.append(field_obj.column_name)

	def _create_sqls(self) -> list[str]:
		"""Build the DDL for the FTS5 table and, when synced, its sync triggers."""
		columns = ", ".join(self._field_columns)
		source = self.model_cls.table_name
		pk_col = self.model_cls._pk_field.column_name
		fts = self.fts_table
		if not self.content_sync:
			return [f"CREATE VIRTUAL TABLE IF NOT EXISTS {fts} USING fts5({columns}, tokenize={self.tokenizer!r})"]

		new_cols = ", ".join(f"new.{c}" for c in self._field_columns)
		old_cols = ", ".join(f"old.{c}" for c in self._field_columns)
		return [
			f"CREATE VIRTUAL TABLE IF NOT EXISTS {fts} "
			f"USING fts5({columns}, content={source!r}, "
			f"content_rowid={pk_col!r}, tokenize={self.tokenizer!r})",
			f"""CREATE TRIGGER IF NOT EXISTS {fts}_ai AFTER INSERT ON {source} BEGIN
                INSERT INTO {fts}(rowid, {columns}) VALUES (new.{pk_col}, {new_cols});
            END""",
			f"""CREATE TRIGGER IF NOT EXISTS {fts}_ad AFTER DELETE ON {source} BEGIN
                INSERT INTO {fts}({fts}, rowid, {columns}) VALUES ('delete', old.{pk_col}, {old_cols});
            END""",
			f"""CREATE TRIGGER IF NOT EXISTS {fts}_au AFTER UPDATE ON {source} BEGIN
                INSERT INTO {fts}({fts}, rowid, {columns}) VALUES ('delete', old.{pk_col}, {old_cols});
                INSERT INTO {fts}(rowid, {columns}) VALUES (new.{pk_col}, {new_cols});
            END""",
		]

	def create(self) -> None:
		"""Create the FTS5 virtual table (and sync triggers)."""
		for sql in self._create_sqls():
			Database.execute(sql)

	async def acreate(self) -> None:
		"""Async version of `create`."""
		await awrite(self.create)

	def drop(self) -> None:
		"""Drop the FTS5 virtual table and its triggers."""
		for suffix in ("ai", "ad", "au"):
			Database.execute(f"DROP TRIGGER IF EXISTS {self.fts_table}_{suffix}")
		Database.execute(f"DROP TABLE IF EXISTS {self.fts_table}")

	async def adrop(self) -> None:
		"""Async version of `drop`."""
		await awrite(self.drop)

	def rebuild(self) -> None:
		"""Rebuild the FTS index from the source table data."""
		Database.execute(f"INSERT INTO {self.fts_table}({self.fts_table}) VALUES ('rebuild')")

	async def arebuild(self) -> None:
		"""Async version of `rebuild`."""
		await awrite(self.rebuild)

	def optimize(self) -> None:
		"""Run FTS5 merge optimization."""
		Database.execute(f"INSERT INTO {self.fts_table}({self.fts_table}) VALUES ('optimize')")

	async def aoptimize(self) -> None:
		"""Async version of `optimize`."""
		await awrite(self.optimize)

	def search(self, query: str, *, limit: int | None = None, offset: int | None = None) -> list[Model]:
		"""Search the index; returns model instances ordered by relevance.

		Args:
		    query: FTS5 match expression (e.g. `"python async"`).
		    limit: Maximum results to return.
		    offset: Number of results to skip.
		"""
		if not query or not query.strip():
			return []
		source = self.model_cls.table_name
		fts = self.fts_table
		sql = (
			f"SELECT {source}.* FROM {source} "
			f"INNER JOIN {fts} ON {source}.{self.model_cls._pk_field.column_name} = {fts}.rowid "
			f"WHERE {fts} MATCH ? ORDER BY {fts}.rank"
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
		return [self.model_cls._from_row(dict(row)) for row in Database.fetchall(sql, params)]

	async def asearch(self, query: str, **kwargs: Any) -> list[Model]:
		"""Async version of `search`."""
		return await athread(functools.partial(self.search, query, **kwargs))

	def search_count(self, query: str) -> int:
		"""Return the number of rows matching the FTS query."""
		if not query or not query.strip():
			return 0
		fts = self.fts_table
		count = Database.fetch_value(f"SELECT COUNT(*) AS cnt FROM {fts} WHERE {fts} MATCH ?", [query], column="cnt")
		return int(count or 0)

	async def asearch_count(self, query: str) -> int:
		"""Async version of `search_count`."""
		return await athread(self.search_count, query)

	def __repr__(self) -> str:
		"""Return a debug representation naming the model, table, and fields."""
		return f"<SearchIndex model={self.model_cls.__name__!r} table={self.fts_table!r} fields={self.fields!r}>"


__all__ = ["SearchIndex"]
