# Optional external snapshot and facts projection phases

External capture is currently a standalone helper; rehearsal preparation does not
invoke it. The helper accepts machine-local `.brain/local.json` declarations:

```json
{"rehearse_external": {"version": 1, "roots": ["/example/project", "/example/project-work"]}}
```

Each root must exactly match a registered node home, worktree path or workbench.
Roots must exist, be separate from the instance and each other, and have no
symbolic link parents. Preparation captures them once with independent storage
and detached Git metadata. Supported bindings below each root are redirected to
the capture for both comparison lanes. Outward links refuse. Unknown bindings
remain covered by drift detection. Copy-time consistency across files or roots
is not an atomic transaction; preparation requires a coherent source capture.
No capture is performed without the declaration. Declaring roots alone does not
enable capture in the rehearsal command; that integration is deferred.

For a separate generated-facts phase, set `status_projection=owned-facts` on a
node, then invoke `brain status app --project`. A scheduler or coordinator may
explicitly own this phase; configuration alone starts no service. Only the
facts markers grant generated-block ownership. Source prose, active work and
archives are retained byte for byte. Active or interrupted journal writers,
malformed markers and unconfigured nodes refuse. Cleanup never invokes it.
