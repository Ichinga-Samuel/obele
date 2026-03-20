"""obele.kv - Persistent key-value storage.

Provides :class:`KVStore` (general-purpose) and :class:`KV` (global singleton).
"""

from .store import KVStore
from .globals import KV

__all__ = ["KVStore", "KV"]

