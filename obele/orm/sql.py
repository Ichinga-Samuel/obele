"""Small SQL helpers shared by the ORM and key-value store."""

from __future__ import annotations

import re

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_identifier(name: str, *, kind: str = "identifier") -> str:
	"""Validate a SQLite identifier used in generated SQL."""
	if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
		raise ValueError(
			f"{kind} must be a valid SQLite identifier (letters, digits, underscores, starting with a letter or underscore), got {name!r}"
		)
	return name
