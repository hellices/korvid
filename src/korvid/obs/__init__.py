"""Read-only observability connectors (issue #193).

`korvid.obs` asks a fixed catalogue of resource-scoped questions of an
external observability backend and returns a bounded, self-describing
answer. It is not an observability client: there is no free-form query
surface, and every call passes the same limit gate before a request
leaves the process.

The boundary (`connector`) is stdlib-only. The two HTTP implementations
(`prometheus`, `loki`) import `httpx` and ship in the `observability`
extra.
"""
