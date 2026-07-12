# Signals, Relationships, and Errors

## Reverse Relationship Objects

`ForeignKeyField` installs a descriptor on the referenced model. Accessing the
descriptor through an instance returns a manager already filtered to that
instance. Applications normally encounter these objects indirectly through a
`related_name`.

::: obele.ReverseRelationDescriptor

::: obele.ReverseRelationManager

## Signals

Model instance `save()` emits `pre_save` and `post_save`. A new row also emits
`pre_create` and `post_create`. A physical instance deletion emits
`pre_delete` and `post_delete`. QuerySet bulk `update()` and `delete()` operate
directly in SQL and do not emit per-instance signals.

::: obele.Signal

::: obele.receiver

The six shared signal objects are `pre_save`, `post_save`, `pre_create`,
`post_create`, `pre_delete`, and `post_delete`.

## Exceptions

All library exceptions derive from `ORMError`. `IntegrityError` is also a
`DatabaseError`, allowing callers to catch constraint failures specifically or
all wrapped SQLite failures generally.

::: obele.ORMError

::: obele.FieldValidationError

::: obele.RecordNotFoundError

::: obele.MultipleResultsError

::: obele.DatabaseError

::: obele.IntegrityError

::: obele.MigrationError

::: obele.ConfigurationError
