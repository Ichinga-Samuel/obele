"""Async-specific tests for enhanced ORM features."""

from __future__ import annotations

from obele import Database, IntegerField, Model, TextField


class TestAsyncEnhancements:
    async def test_async_database_using_scope_works(self, tmp_path):
        global_db = tmp_path / "async_global.sqlite3"
        scoped_db = tmp_path / "async_scoped.sqlite3"

        await Database.aconfigure(str(global_db))
        await Database.aexecute("CREATE TABLE global_items (id INTEGER PRIMARY KEY, val TEXT)")
        await Database.aexecute("INSERT INTO global_items (val) VALUES (?)", ["global"])

        async with Database.using(str(scoped_db)):
            path, _ = Database.current_config()
            assert path == str(scoped_db)
            await Database.aexecute("CREATE TABLE scoped_items (id INTEGER PRIMARY KEY, val TEXT)")
            await Database.aexecute("INSERT INTO scoped_items (val) VALUES (?)", ["scoped"])

        path, _ = Database.current_config()
        assert path == str(global_db)
        row = (await Database.aexecute_read("SELECT val FROM global_items")).fetchone()
        assert row["val"] == "global"

    async def test_async_migrate_adds_column_and_backfills_default(self):
        class LegacyThing(Model):
            table_name = "async_things"
            name = TextField()

        class MigratedThing(Model):
            table_name = "async_things"
            name = TextField()
            priority = IntegerField(default=7)

        await Database.aconfigure(":memory:")
        await LegacyThing.acreate_table()
        await LegacyThing.acreate(name="Task")

        await MigratedThing.amigrate()

        thing = await MigratedThing.aget(name="Task")
        assert thing.priority == 7

