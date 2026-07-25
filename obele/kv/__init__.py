"""obele.kv - Persistent key-value storage.

Provides `KVStore` (general-purpose) and `KV` (global singleton).
"""

from .globals import KV
from .store import KVStore

__all__ = ["KVStore", "KV"]
