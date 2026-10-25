# Code decisions

## D001: Node-local full gates and native machine capacity

Full-gate exclusion is keyed by the resolved node id within an instance. The
stable lock is `.brain/gates/<sha256-node-id>.lock`; display labels do not define
identity. `gate-lock --node <node>` selects a node explicitly. Without that option,
node folders, registered homes, workbenches and worktrees are ranked together by
the most specific containing path. Repeated anchors for one node are deduplicated;
equal-depth matches for different nodes and unbound context refuse. A containing
node folder cannot mask a deeper registered home or workbench.

Native monolithic verification and lane verification acquire the node lock before
joining machine admission. A lane run retains the node lock across every command,
while each command joins the native FIFO queue afresh. Different nodes can run
concurrently without same-node waiters consuming machine slots. Capacity remains
in the existing machine policy: configurable `verify_day` and `verify_night`, with
defaults of two daytime slots and one nighttime slot. A gate wrapper does not
reserve an additional machine slot.

Nested commands retain the same node's descriptor through exec and explicit
`pass_fds`. Inherited proof binds the node id, stable path, regular-file inode and
held open-file-description lock; incomplete, closed, stale, unlocked or mismatched
proof refuses. Owners close their descriptors without explicitly unlocking, so a
surviving verification child retains exclusion. Lock files are never replaced or
unlinked during normal operation. Lock-path safety checks reject linked and
nonregular targets without interfering with scoped verification-log cleanup.

Regression tests use isolated instances and controlled process barriers to prove
same-node exclusion, different-node overlap, configured capacities, inherited
ownership and cleanup, without generating system load.

Isolated scenario fixtures temporarily remove only enclosing gate environment
proof from both their subprocess environment and in-process environment, restoring
it during cleanup. They retain the enclosing descriptor and lock. Runtime
cross-node proof refusal remains unchanged. An in-repository regression runs
existing subprocess and native lane fixtures under a real gate wrapper, then
checks that enclosing proof is restored and its descriptor still owns the lock.

## D002: Select the initial job runtime before the first ledger append

Resolve a new worktree job's runtime with the existing `worker_route` resolver
before `job_started` appends its first ledger row. The initial row then reflects
the creation-time builder route, including existing runtime pins, class defaults
and inherited model selection.

A later brief's explicit `--runtime` or `--model` choice remains a later route
update through `record_job_route`. It does not change the original committed
ledger revision. Reusing the resolver preserves runtime/model precedence and
model-aware subscription selection; this change adds no flags or routing semantics.

The regression reads the first committed ledger revision before generating a
brief, then checks that the brief retains the expected runtime and model. Its
cases cover class defaults, a node runtime pin, node and instance class routing,
and inherited model configuration without activating inherited runtime policy.

## D1001: Keep governance identifiers out of content screening and worker output

Exact configured principal identifiers in library and specialist scope metadata
are governance values. Screen all other metadata and content, including duplicate
governance keys, with the existing never-name checks. Use the same frontmatter
grammar for parsing and streaming scans. Limit on-disk exemptions to recognized
instance library, role and lesson paths; external copies and ordinary files stay
strict, including Git index blobs and symlinks.

Proposal acceptance, hiring, recall and connection discovery apply this distinction
without changing stored scopes. Worker-facing item output removes governance
lines when those lines contain a forbidden principal. Final briefs remain subject
to strict content checks.

## D1002: Validate decision scope and external fact citations

Dated entries in node decision logs carry an explicit `Applies to:` scope.
`check` warns on missing scope and on external fact claims without a `Source:`
URL or `probe:` command. Explicit fact labels and common vendor/provider policy
phrasing trigger the citation check. This validates evidence metadata only; it
never executes a probe or claims that a cited source proves the statement.

`decision_scope_since` on a node or instance can exempt earlier dated entries.
Without a cutoff, all dated entries are checked. Template examples are ignored.

## D1003: Enforce declared tool patterns with exact single-use allowances

`hook pretool` matches explicit `Forbidden actions` bullets in node instructions,
inheriting only to the nearest ownership boundary. Shell bullets are conservative
textual glob searches over a whole command, including compound commands; `tool:`
patterns match tool identities. This declared subset is not a shell sandbox.

`allow --once` records an exact shell command, resolved working directory and node.
It does not run the command. A locked atomic state update consumes the allowance
before returning permission, with opaque issued/consumed audit events. Policy
changes invalidate allowances. Hook errors fail open, matching existing runtime
hook behavior. Runtime adapter templates register the hook; updating an adapter
file does not install or activate it on a machine.

An optional worker-message validator receives only explicit host session identity
and raw tool arguments; the action engine never guesses a caller's role.

## D1004: Expire explicit operational limits without granting outward consent

A limit may declare one absolute `Expires:` timestamp. Brief generation omits the
limit after that time, without changing its source record. Invalid or duplicate
expiry metadata keeps the limit active and produces a check warning. Narrative
`Lift when:` conditions remain explanatory; core does not guess that they hold.

Publication policy continues to snapshot every original limit, including expired
ones. An operational expiry never grants push, publication or deletion consent.

## D1005: Review independence is a fresh non-builder thread identity

Instances and nodes may select `review_independence=fresh-thread`, with optional
`review_runtime` and `review_model`. Same-runtime and same-model reviewers then
qualify when their actual host identity belongs to a distinct, fresh context that
did not build the change. Missing identities or unknown/reused context refuse.
Cross-runtime routing remains the compatibility default. A legacy same-runtime
exception needs an absolute expiry and an explicit reason; it still requires the
fresh-thread identity receipt before readiness.

`review --receipt-file` records a completed review separately from briefing a new
round. A receipt names the exact job generation and code head, observed builder
and reviewer runtime/model/thread, context independence and verdict. Builder
identity must match recorded worker bindings and the builder route. Recording
requires an observation from the last five minutes; the immutable observation
remains valid for the same code. Later code or a blocked verdict needs new proof.
These are coordinator-supplied host observations, not independent live-host queries.

Thread identities and receipt histories live under machine-local `.brain/`.
The shared ledger stores only the local receipt reference and required-proof flag.
Readiness consumes a pure validator so the gate owner can enforce it before work.

## D1006: Bind preview rendering and source comparisons to code and URLs

A configured preview or explicit `requires_preview_evidence=true` requires an
exact-head receipt. Each presented preview/review URL needs a successful rendering
observation and evidence reference, plus three distinct displayed items compared
with their source values and references. Wrong URLs, absent rendering evidence,
fewer than three distinct matches, future observations and stale heads refuse.

The recording command stores supplied browser observations locally; it does not
pretend to open a browser itself. Readiness uses the pure validator. A screenshot
tour receipt and these source comparisons prove different properties.

## D1007: Preserve finding closure until its code inputs change

Findings have stable IDs, failure classes, severity, explicit tracked input paths
and closure evidence. Machine-local state retains closed findings across later
reviews and unrelated commits. A change to a bound Git object reopens the finding;
closing it again requires evidence against the new inputs. Existing IDs cannot
silently rebind their input scope. Review observations retain their original
verdict while the finding-state projection provides durable closure history.

## D1008: Preserve guard scope through policy changes and linked directories

A fresh explicit allowance can replace an obsolete allowance for the same exact
action after the declared rule set changes. The old authorization is recorded as
invalidated before the new one is issued; an unconsumed current-policy allowance
still cannot be duplicated.

Governance exemptions require real instance paths below the instance root.
Linked intermediate library or specialist directories never grant an exemption
to foreign file contents, even when the final file itself is regular.

## D1009: Apply receipt policies before verification and on packet reads

Readiness calls the independently owned review and preview validators before any
lane mapping or verifier starts, and again when reading normal, display or train
packets. Required policies refuse if their validator API is unavailable. Default
unconfigured nodes keep their existing behavior. A typed configuration reader
treats only a real instance settings dictionary as configuration.

Presented `review_urls` are machine-local, like `preview`; the settings command
accepts a JSON array string and the receipt validator checks every configured URL.
The schema integration uses actual receipt writers and a fabricated v3 hand-back,
with a fake verifier, so it proves admission and read enforcement without running
a real gate or claiming browser observations.

## D1010: Revalidate receipt policy inside the display cache

A display-cache hit rechecks required receipt policy against the cached code head.
Policy-only changes replace only the prior policy reasons and readiness flag;
Git and input facts keep their existing cache identity. Restoring valid evidence
clears a cached policy refusal without rerunning unrelated validation. Cache-private
provenance is held as dictionary attributes, outside serialized result fields.
Policy reasons are privacy-screened before entering cache provenance or output.
Full and train validations continue to bypass the display cache.

## D1011: Screen shared retrieval candidates with typed governance metadata

Lexical ranking and optional local semantic retrieval share the same candidate
iterator. It applies state, scope and source-content checks once, masking only
exact known principal values in governance fields. Body text and unknown values
remain screened. Query ranking does not bypass candidate eligibility.

## D1012: Reconcile readiness with the durable finding union

A passing exact-head identity receipt also requires a readable durable finding
union. Omission preserves earlier findings; open or input-reopened majors and
blockers refuse readiness. Closed findings survive unrelated commits. Relevant
changes, including deletion, require an explicit evidence-backed closure at the
new commit. Finding scope, severity and class cannot be rebound under the same ID.
Persisted records validate their schema and input hashes against their recorded
commit before either readiness reads or receipt writes can trust them. Missing
state after a recorded identity review refuses replacement rather than resetting
its history.
