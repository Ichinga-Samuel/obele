# Key-Value Store

`KVStore` is a persistent mutable mapping backed by SQLite. It supports typed
keys, namespaces, TTL, serializers, atomic operations, prefix/range/scan
queries, batch operations, and async equivalents.

::: obele.KVStore

`KV` is a singleton convenience wrapper around `KVStore`.

::: obele.KV
