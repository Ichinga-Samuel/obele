"""Custom exception hierarchy for the ORM."""


class ORMError(Exception):
	"""Base exception for all ORM errors."""


class FieldValidationError(ORMError):
	"""Raised when a field value fails validation (type mismatch, constraint violation)."""


class RecordNotFoundError(ORMError):
	"""Raised when a query expected exactly one result but found none."""


class MultipleResultsError(ORMError):
	"""Raised when a query expected exactly one result but found multiple."""


class DatabaseError(ORMError):
	"""Wraps sqlite3.Error for ORM-level handling."""


class IntegrityError(DatabaseError):
	"""Wraps sqlite3.IntegrityError for constraint violations."""


class MigrationError(ORMError):
	"""Raised when a schema migration fails."""


class ConfigurationError(ORMError):
	"""Raised when Database.configure() is called with invalid arguments."""
