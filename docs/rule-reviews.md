# Rules in contracts and reviews

Generated worker, small-job, import, specialist and review briefs carry inherited
rules verbatim. The startup wording under `## Instance rules` is the minimum;
full indexed rules remain in `library/instance-rules.md` under `<!-- rule:R01 -->`
markers. Rules under `## Rules for this node` travel with inherited parent rules,
up to the nearest ownership boundary. Generated contract companions (`<job-key>.rules.md`)
contain the same rules. The authoritative `<job-key>.md` retains the coordinator's
contract bytes for task launch checks and metadata readers. Briefs render that
contract with the current rules; companions are refreshed when contracts are recorded.
Never copy instance instruction text into a project's source repository.

Only structural `Applies: all` or `Applies: node-id, other-id` annotations filter
rules. Missing annotations mean all nodes. Instructions are never selected by
keywords in their wording. Briefs retain source locations and IDs: indexed rules
keep their `R01` IDs; node bullets use `node:<node-id>:<explicit-marker-or-content-id>`.
Generated snapshots use reserved `brain:applicable-rules` comment delimiters and
are replaced when a brief is regenerated. Mandatory rules do not consume the
task/context byte allowance, but still count toward the absolute brief ceiling.
Rule declarations are top-level Markdown bullets under the named rule headings;
section prefaces and reference pointers are not separate rule declarations.
Inline comments stay verbatim in the brief but do not hide bullets or change
their content IDs. Source wording, including comments, still binds the digest.

Every review returns the JSON `rule_checks` table described in
[the review skill](../skills/review/SKILL.md). Copy the brief's `rules_digest` and
table into the existing review identity receipt submitted with
`brain review <node> --branch <branch> --receipt-file <file>`.
Rules require an exact-head independent receipt even under the legacy
cross-runtime policy. Generated review briefs record their rules digest, including
an empty rule set; legacy jobs without rules and without a review snapshot retain their
existing receipt policy. Readiness checks current rule text and applicability, so changed
instructions require fresh review evidence. Pass/fail evidence must parse as
`file:positive-line`; n/a needs a reason. This checks the report's structure;
the independent reviewer remains responsible for judging compliance.
