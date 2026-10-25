# Shared service ownership

Runners that share a database, preview or service can declare canonical keys:

```
verify_resources: database:demo-checks,preview:demo-local
```

Wrap the complete setup, work and cleanup in
`configured_resource_lease(instance, node)`. An absent setting changes nothing.
All consumers must agree on the same key; aliases are distinct resources.

The lower-level `resource_lease(instance, node_id, keys)` yields claim receipts.
`resource_child(receipt, pid)` adds a locally owned service process whose exact
birth identity must survive parent loss. Never register an unrelated process or
Docker daemon as a service child.

Persistent leases survive owner death, reboot and failed bodies: those events do
not establish database/preview cleanup. Once cleanup is confirmed, the owning
process can call `resource_release(receipt)`. A busy or uncertain child prevents
release. A lost persistent owner requires explicit recovery inspection; this API
does not guess that recovery is safe or provide a force-clear shortcut.

For an entirely process-bound resource, callers may explicitly use
`resource_claim(instance, node_id, key, lifetime='process')`. Only this narrower
mode can reclaim after all recorded identities are gone or a known boot changes.
No API stops services, signals unrelated processes, or deletes application data.
