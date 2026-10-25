# Source-first diagnostics

`brain diagnose <node> plan --branch <job> --question <question> --source path:line
--finding source|ambiguous|runtime --reason <answer-or-ambiguity>` records a source
finding against the open job's actual checkout HEAD. Repeat `--source` for more
references. Each reference must identify a tracked regular file and an existing
line in that commit; symlinks, traversal and untracked files are refused. The
stored blob IDs make the source snapshot explicit. Dirty files are not canonical
committed evidence.

A `source` finding returns the recorded answer with `probe_allowed: false`.
`ambiguous` and `runtime` require a stated evidence gap and return one persistent
pending attempt ID. Repeating a plan reuses that pending ID. Record its outcome:

```
brain diagnose app outcome --branch job/example --question 'Which setting?' --attempt <id> --state setup-failed
```

The other outcome is `observed`. An outcome must identify the current pending
attempt. After two failed setups for the same job generation, HEAD and question,
a third setup is refused. Prior receipts remain intact; rule from those sources
or narrow the question. A new job or actual HEAD change creates another evidence
episode. State lives only in the instance's private `.brain/diagnostics/` folder.

The caller supplies the source-readability classification; the core does not
invent a semantic answer from prose. This command enforces the supported permit
path and never executes shell commands, builds a harness or intercepts external
commands. Canonical test execution is a separate command. No host, test or
publication permission is granted by a diagnostic permit.
