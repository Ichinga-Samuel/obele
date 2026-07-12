# Migration CLI

Obele installs the `obele-orm` command. The equivalent module form is
`python -m obele.orm`.

The CLI performs schema synchronization from current model definitions. It is
not a versioned migration-history framework: it does not generate migration
files or remember which migrations ran.

## Discover Models

Use `--module` to import a module and discover model classes declared directly
inside it:

```bash
obele-orm list-models --module myapp.models
```

Use `--model` for an explicit class. Both accepted forms are shown below:

```bash
obele-orm list-models \
  --model myapp.models:User \
  --model myapp.models.Product
```

Selectors may be repeated and combined. Duplicate classes are removed, and
the output is ordered so foreign-key targets appear before dependants.

## Synchronize a Database

```bash
obele-orm migrate \
  --database app.sqlite3 \
  --module myapp.models
```

For each discovered model, the command calls `Model.migrate()`. A missing
table is created. An existing table is transactionally rebuilt from the model
definition, preserving columns with matching names and removing columns no
longer declared.

To rename a field without losing its old column data, map the new model field
to the previous physical column:

```bash
obele-orm migrate \
  --database app.sqlite3 \
  --module myapp.models \
  --rename User.display_name=full_name
```

When short class names are ambiguous, use a qualified model reference:

```bash
--rename myapp.models:User.display_name=full_name
```

Additional SQLite pragmas can be repeated. Values are parsed as booleans,
null, integers, floats, or strings:

```bash
obele-orm migrate \
  --database app.sqlite3 \
  --module myapp.models \
  --pragma cache_size=-16000 \
  --pragma foreign_keys=true
```

Use `--no-create-if-missing` to fail when a table does not exist and `--quiet`
to suppress per-model success lines.

## Migration Rules

Obele copies an existing column when its physical name matches the new field,
or when `--rename` identifies the old name. A new column is safe when it is a
primary key, nullable, has a database default, or has a Python default that can
be evaluated and validated. A new required column without a default raises
`MigrationError` rather than inserting invalid data.

The rebuild happens in a transaction: the table is renamed to a temporary
name, the new schema is created, data is copied, the temporary table is
dropped, and indexes are recreated. Back up production databases and review
schema changes before running synchronization.
