"""Command-line tools for SQLite schema-sync migrations."""

from __future__ import annotations

import argparse
import importlib
from types import ModuleType
from typing import Any, Sequence

from .._identity import CLI_PROGRAM, PACKAGE_NAME
from .database import Database
from .model import Model, _toposort_models


def _coerce_scalar(value: str) -> Any:
	"""Convert a string token into a bool, `None`, int, float, or the original string."""
	lowered = value.lower()
	if lowered == "true":
		return True
	if lowered == "false":
		return False
	if lowered == "null":
		return None
	try:
		return int(value)
	except ValueError:
		pass
	try:
		return float(value)
	except ValueError:
		return value


def _parse_key_value(raw: str, *, option_name: str) -> tuple[str, Any]:
	"""Parse a `KEY=VALUE` string into a key and coerced scalar value.

	Args:
		raw: The raw option text in `KEY=VALUE` form.
		option_name: Name of the CLI option, used in the error message.

	Raises:
		ValueError: If `raw` is not in `KEY=VALUE` format.
	"""
	key, sep, value = raw.partition("=")
	if not sep or not key:
		raise ValueError(f"{option_name} must be in KEY=VALUE format")
	return key, _coerce_scalar(value)


def _parse_rename_spec(raw: str) -> tuple[str, str, str]:
	"""Parse a `MODEL.FIELD=OLD_COLUMN` spec into model reference, field name, and old column.

	Raises:
		ValueError: If `raw` is not in `MODEL.FIELD=OLD_COLUMN` format.
	"""
	target, sep, old_column = raw.partition("=")
	if not sep or not old_column:
		raise ValueError("--rename must be in MODEL.FIELD=OLD_COLUMN format")
	model_ref, dot, field_name = target.rpartition(".")
	if not dot or not model_ref or not field_name:
		raise ValueError("--rename must be in MODEL.FIELD=OLD_COLUMN format")
	return model_ref, field_name, old_column


def _qualified_model_name(model_cls: type[Model]) -> str:
	"""Return the module-qualified name of a model as `module:ClassName`."""
	return f"{model_cls.__module__}:{model_cls.__name__}"


def _import_modules(module_names: Sequence[str]) -> list[ModuleType]:
	"""Import each named module, preserving order.

	Raises:
		ValueError: If any module cannot be imported.
	"""
	modules: list[ModuleType] = []
	for module_name in module_names:
		try:
			modules.append(importlib.import_module(module_name))
		except ImportError as exc:
			raise ValueError(f"Could not import module {module_name!r}") from exc
	return modules


def _models_from_module(module: ModuleType) -> list[type[Model]]:
	"""Return `Model` subclasses declared directly in `module`, excluding `Model` itself and imported ones."""
	models: list[type[Model]] = []
	for value in vars(module).values():
		if isinstance(value, type) and issubclass(value, Model) and value is not Model and value.__module__ == module.__name__:
			models.append(value)
	return models


def _resolve_model_spec(spec: str) -> type[Model]:
	"""Resolve a `package.module:ClassName` or `package.module.ClassName` spec to a `Model` subclass.

	Raises:
		ValueError: If the spec is malformed or does not resolve to a `Model` subclass.
	"""
	module_name: str
	class_name: str
	if ":" in spec:
		module_name, class_name = spec.split(":", 1)
	else:
		module_name, dot, class_name = spec.rpartition(".")
		if not dot:
			raise ValueError("Model specs must use 'package.module:ClassName' or 'package.module.ClassName'")
	module = _import_modules([module_name])[0]
	model_cls = getattr(module, class_name, None)
	if not isinstance(model_cls, type) or not issubclass(model_cls, Model) or model_cls is Model:
		raise ValueError(f"{spec!r} does not resolve to a Model subclass")
	return model_cls


def _dedupe_models(models: Sequence[type[Model]]) -> list[type[Model]]:
	"""Return the models with duplicates removed, keeping first occurrence by qualified name."""
	seen: set[str] = set()
	unique: list[type[Model]] = []
	for model_cls in models:
		key = _qualified_model_name(model_cls)
		if key in seen:
			continue
		seen.add(key)
		unique.append(model_cls)
	return unique


def _discover_models(module_names: Sequence[str], model_specs: Sequence[str]) -> list[type[Model]]:
	"""Discover and dedupe models from imported modules and explicit specs.

	Raises:
		ValueError: If no models are discovered.
	"""
	modules = _import_modules(module_names)
	discovered: list[type[Model]] = []
	for module in modules:
		discovered.extend(_models_from_module(module))
	for spec in model_specs:
		discovered.append(_resolve_model_spec(spec))
	models = _dedupe_models(discovered)
	if not models:
		raise ValueError("No models were discovered. Pass --module and/or --model.")
	return models


def _resolve_model_reference(model_ref: str, models: Sequence[type[Model]]) -> type[Model]:
	"""Resolve a bare class name or qualified name against the discovered models.

	Raises:
		ValueError: If `model_ref` matches no model or is ambiguous across models.
	"""
	matches = [model_cls for model_cls in models if model_ref in {model_cls.__name__, _qualified_model_name(model_cls)}]
	if not matches:
		raise ValueError(f"Unknown model reference {model_ref!r} in rename spec")
	if len(matches) > 1:
		raise ValueError(f"Ambiguous model reference {model_ref!r}; use module-qualified syntax")
	return matches[0]


def _group_renames(rename_specs: Sequence[str], models: Sequence[type[Model]]) -> dict[type[Model], dict[str, str]]:
	"""Group `MODEL.FIELD=OLD_COLUMN` specs into a per-model field-to-old-column mapping.

	Raises:
		ValueError: If a spec references an unknown model or field.
	"""
	grouped: dict[type[Model], dict[str, str]] = {}
	for raw_spec in rename_specs:
		model_ref, field_name, old_column = _parse_rename_spec(raw_spec)
		model_cls = _resolve_model_reference(model_ref, models)
		if field_name not in model_cls._fields:
			raise ValueError(f"Unknown field {field_name!r} for model {_qualified_model_name(model_cls)}")
		grouped.setdefault(model_cls, {})[field_name] = old_column
	return grouped


def _build_parser() -> argparse.ArgumentParser:
	"""Build the argument parser for the `migrate` and `list-models` subcommands."""
	parser = argparse.ArgumentParser(prog=CLI_PROGRAM, description=f"SQLite schema-sync tooling for {PACKAGE_NAME}.orm models.")
	subparsers = parser.add_subparsers(dest="command", required=True)

	def add_model_args(command: argparse.ArgumentParser, *, require_database: bool) -> None:
		"""Add shared `--module`/`--model` (and optionally `--database`/`--pragma`) arguments to a subparser."""
		if require_database:
			command.add_argument("--database", required=True, help="SQLite database path")
			command.add_argument(
				"--pragma",
				action="append",
				default=[],
				metavar="KEY=VALUE",
				help="Additional SQLite pragmas to apply before running the command",
			)
		command.add_argument("--module", action="append", default=[], help="Import a module and discover Model subclasses declared in it")
		command.add_argument("--model", action="append", default=[], help="Explicit model path in package.module:ClassName format")

	migrate_parser = subparsers.add_parser("migrate", help="Schema-sync all discovered models into the target SQLite database")
	add_model_args(migrate_parser, require_database=True)
	migrate_parser.add_argument(
		"--rename",
		action="append",
		default=[],
		metavar="MODEL.FIELD=OLD_COLUMN",
		help="Map a new field name to an old column name during migration",
	)
	migrate_parser.add_argument("--no-create-if-missing", action="store_true", help="Fail instead of creating a missing table")
	migrate_parser.add_argument("--quiet", action="store_true", help="Suppress per-model progress output")

	list_parser = subparsers.add_parser("list-models", help="List models discovered from the provided module and model selectors")
	add_model_args(list_parser, require_database=False)

	return parser


def _ensure_selection(args: argparse.Namespace) -> None:
	"""Validate that at least one `--module` or `--model` selector was provided.

	Raises:
		ValueError: If neither selector is present.
	"""
	if not args.module and not args.model:
		raise ValueError("At least one --module or --model argument is required")


def _parse_pragmas(values: Sequence[str]) -> dict[str, Any]:
	"""Parse a sequence of `KEY=VALUE` pragma strings into a mapping of coerced values."""
	pragmas: dict[str, Any] = {}
	for raw in values:
		key, value = _parse_key_value(raw, option_name="--pragma")
		pragmas[key] = value
	return pragmas


def _run_list_models(args: argparse.Namespace) -> int:
	"""Print each discovered model in dependency order and return an exit code of 0."""
	_ensure_selection(args)
	for model_cls in _toposort_models(_discover_models(args.module, args.model)):
		print(f"{_qualified_model_name(model_cls)} [{model_cls.table_name}]")
	return 0


def _run_migrate(args: argparse.Namespace) -> int:
	"""Schema-sync all discovered models into the target database in dependency order.

	Configures the database with parsed pragmas, migrates each model applying any
	rename mappings, and always closes the connection. Returns an exit code of 0.
	"""
	_ensure_selection(args)
	models = _toposort_models(_discover_models(args.module, args.model))
	rename_map = _group_renames(args.rename, models)
	pragmas = _parse_pragmas(args.pragma)

	Database.configure(args.database, pragmas=pragmas)
	try:
		for model_cls in models:
			model_cls.migrate(rename_fields=rename_map.get(model_cls), create_if_missing=not args.no_create_if_missing)
			if not args.quiet:
				print(f"migrated {_qualified_model_name(model_cls)} [{model_cls.table_name}]")
		return 0
	finally:
		Database.close()


def main(argv: Sequence[str] | None = None) -> int:
	"""Parse arguments and dispatch to the selected subcommand.

	Args:
		argv: Argument list to parse; defaults to `sys.argv` when `None`.

	Returns:
		The process exit code; `ValueError` from a command is reported via
		`parser.error` (which exits with status 2).
	"""
	parser = _build_parser()
	args = parser.parse_args(list(argv) if argv is not None else None)
	try:
		if args.command == "migrate":
			return _run_migrate(args)
		if args.command == "list-models":
			return _run_list_models(args)
	except ValueError as exc:
		parser.error(str(exc))
	return 1


__all__ = ["main"]
