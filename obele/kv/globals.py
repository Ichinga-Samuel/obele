"""Global singleton key-value store.

The :class:`KV` class provides a convenient, process-wide singleton wrapper
around :class:`~obele.kv.store.KVStore`.  The first instantiation creates
the underlying store; subsequent instantiations return the **same** instance
(any constructor arguments are silently ignored).  Call :meth:`KV.reset` to
discard the current instance so that the next ``KV(...)`` call creates a fresh
one.

Usage::

    from obele.kv import KV

    # First call - creates the store
    kv = KV("app_settings", key_type=str)
    kv["theme"] = "dark"

    # Subsequent calls - same instance, args ignored
    kv2 = KV("ignored_table_name")
    assert kv2["theme"] == "dark"
    assert kv is kv2

    # Reset to allow re-creation
    KV.reset()
    kv3 = KV("new_table")   # fresh store
"""

from __future__ import annotations

import threading
from typing import Any

from .store import KVStore, _Dumps, _Loads, SerializerMode


class KV(KVStore):
    """Singleton :class:`KVStore` subclass.

    The first ``KV(...)`` call creates the global instance using the
    supplied arguments.  Every subsequent call returns that same instance,
    ignoring any new arguments, until :meth:`reset` is called.

    Inherits the full :class:`KVStore` API - dict access, slicing,
    multi-key queries, range queries, async methods, etc.
    """

    _instance: KV | None = None
    _lock: threading.Lock = threading.Lock()

    def __new__(
        cls,
        table_name: str = "kv_default",
        *,
        key_type: type[Any] | None = None,
        enforce_key_type: bool = True,
        serializer: SerializerMode | tuple[_Dumps, _Loads] = "auto",
    ) -> KV:
        # Fast path - no lock needed for reads.
        if cls._instance is not None:
            return cls._instance

        with cls._lock:
            # Double-check after acquiring lock.
            if cls._instance is not None:
                return cls._instance

            instance = super().__new__(cls)
            cls._instance = instance
        return instance

    def __init__(
        self,
        table_name: str = "kv_default",
        *,
        key_type: type[Any] | None = None,
        enforce_key_type: bool = True,
        serializer: SerializerMode | tuple[_Dumps, _Loads] = "auto",
    ) -> None:
        if getattr(self, "_kv_initialized", False):
            return
        super().__init__(
            table_name=table_name,
            key_type=key_type,
            enforce_key_type=enforce_key_type,
            serializer=serializer,
        )
        self._kv_initialized = True

    @classmethod
    def reset(cls) -> None:
        """Discard the current singleton instance.

        The next ``KV(...)`` call will create a brand-new :class:`KVStore`
        with whatever arguments are provided.
        """
        with cls._lock:
            cls._instance = None

    def __repr__(self) -> str:
        if not getattr(self, "_kv_initialized", False):
            return "<KV (not initialised)>"
        return (
            f"<KV table={self._table!r} size={len(self)} "
            f"key_type={self._resolved_key_type.__name__ if self._resolved_key_type else 'unset'} "
            f"enforce={self._enforce}>"
        )

