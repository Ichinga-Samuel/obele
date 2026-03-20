"""Tests for the SQLite schema-sync CLI."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    pythonpath_parts = [str(tmp_path)]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    return subprocess.run(
        [sys.executable, "-m", "obele.orm", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class TestOrmCli:
    def test_migrate_command_syncs_schema_from_module(self, tmp_path: Path):
        db_path = tmp_path / "cli.sqlite3"
        module_path = tmp_path / "cli_models.py"
        module_path.write_text(
            textwrap.dedent(
                """
                from obele import BooleanField, IntegerField, Model, TextField


                class CliUser(Model):
                    table_name = "cli_users"
                    name = TextField()
                    age = IntegerField(default=30)
                    active = BooleanField(default=True, index=True)
                """
            ),
            encoding="utf-8",
        )

        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE cli_users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL)")
        conn.execute("INSERT INTO cli_users (name) VALUES (?)", ["Alice"])
        conn.commit()
        conn.close()

        result = _run_cli(
            tmp_path,
            "migrate",
            "--database",
            str(db_path),
            "--module",
            "cli_models",
        )

        assert result.returncode == 0, result.stderr
        assert "migrated cli_models:CliUser [cli_users]" in result.stdout

        conn = sqlite3.connect(db_path)
        columns = {
            row[1]: row
            for row in conn.execute("PRAGMA table_info(cli_users)").fetchall()
        }
        row = conn.execute(
            "SELECT name, age, active FROM cli_users WHERE name = ?",
            ["Alice"],
        ).fetchone()
        indexes = conn.execute("PRAGMA index_list(cli_users)").fetchall()
        conn.close()

        assert "age" in columns
        assert "active" in columns
        assert row == ("Alice", 30, 1)
        assert any(index[1] == "idx_cli_users_active" for index in indexes)

    def test_migrate_command_supports_column_renames(self, tmp_path: Path):
        db_path = tmp_path / "rename.sqlite3"
        module_path = tmp_path / "rename_models.py"
        module_path.write_text(
            textwrap.dedent(
                """
                from obele import Model, TextField


                class RenamedUser(Model):
                    table_name = "rename_users"
                    full_name = TextField(column_name="full_name")
                """
            ),
            encoding="utf-8",
        )

        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE rename_users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL)")
        conn.execute("INSERT INTO rename_users (name) VALUES (?)", ["Alice"])
        conn.commit()
        conn.close()

        result = _run_cli(
            tmp_path,
            "migrate",
            "--database",
            str(db_path),
            "--module",
            "rename_models",
            "--rename",
            "RenamedUser.full_name=name",
        )

        assert result.returncode == 0, result.stderr
        assert "migrated rename_models:RenamedUser [rename_users]" in result.stdout

        conn = sqlite3.connect(db_path)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(rename_users)").fetchall()]
        row = conn.execute("SELECT full_name FROM rename_users").fetchone()
        conn.close()

        assert "full_name" in columns
        assert "name" not in columns
        assert row == ("Alice",)

