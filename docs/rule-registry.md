# Rule gates and sweeps

Each section of `library/instance-rules.md` starts with a unique marker, such as
`<!-- rule:R01 -->`, and carries exactly one `Gate:` and one `Applies:` line:

```markdown
<!-- rule:R01 -->
- Keep source receipts with every result.
Gate: tests/test_receipts.py::ReceiptChecks::test_source_is_required
Applies: app
```

`Gate: none (manual source inspection)` records a rule without an automated
gate. A blank reason is invalid. Multiple test references use `; ` separators.
Paths are relative to each applicable node's bound home. Tests must exist there;
outward symlinks and parent traversal fail. Python function and class-method
declarations are checked without importing or executing the file. A bare test
method name works when unique; class-qualified names use `Class::test_method`.
JavaScript and TypeScript gates may name a literal `test("name", ...)` or
`it("name", ...)` declaration, including runner modifiers. Comments and strings
containing example declarations do not count. Computed test names need a literal
named wrapper test. This is an existence check, not evidence that the test passes
or enforces the rule's meaning.

`Applies: all` selects all active nodes with a home on this machine. An explicit
comma-separated list selects just those registered node IDs. Unknown or repeated
IDs fail. Rules in a node's `AGENTS.md` are top-level Markdown bullets under
`## Rules for this node`; append `Gate:` and `Applies:` annotations to the bullet.
These rules stay local: `Applies: all` means that node, and an explicit list may
only name that node. A preceding `<!-- rule:LOCAL-1 -->` gives a stable ID;
otherwise the ID is `N-` plus a digest of the bullet without its annotations.
Changing an unmarked bullet's wording creates a new identity. Fenced examples
and HTML comments do not supply metadata.

`brain check` validates this registry and opens `SWEEP-<rule-id>` tasks with
status `ready` and the verbatim rule text, once for each applicable node home.
The first check opens sweeps for all previously unrecorded rules, including rules
that predate installation. Invalid rules keep diagnostics and remain unacknowledged;
valid rules still open their sweeps. A task ownership collision stops publication.
Unlike passive status/dashboard reads, **plain check can now write tasks**.
It uses the instance writer lock and native task validation. It never contacts
an AI provider or writes to a node's home repository.
Frozen rehearsal read lanes validate without publishing sweeps. Ordinary checks
release the writer lock before diagnostics and live probes; `--fix` retains its
existing lock while applying repairs.

The local checkpoint is `.brain/rule-registry.json`. Tasks are committed before
their rule IDs are acknowledged. If the command stops before acknowledgment,
the next check recognizes matching tasks and finishes the checkpoint. It refuses
an unrelated task with the same ID and refuses corrupt checkpoints. Existing
tasks, including their status, remain intact. Editing an already recorded rule's
metadata does not create another sweep; additions are detected by rule ID.

After upgrading, annotate the instance sections and node bullets before running
check. Every missing annotation is reported by rule ID; no default reason or
scope is inferred from prose. Keep instance annotations and sweep reports in the
instance or private workbench, outside the published core repository.
