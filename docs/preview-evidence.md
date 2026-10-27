# Preview screenshot tours

Configure the preview address and tour path with `brain set <node> preview=<url>
preview_tour=<absolute-index.md>`. Open the preview, inspect its data, capture the
touched screens as PNGs, and link those images from the Markdown tour. Commit the
source being shown so the receipt can name a clean revision. A registered job
branch can supply the source before it lands on main.

Create a private JSON receipt with this shape, using actual paths and digests:

```json
{
  "version": 1,
  "url": "http://127.0.0.1:4109/",
  "branch": "job/navigation",
  "head": "0123456789abcdef0123456789abcdef01234567",
  "checked_at": "2026-01-15T12:00:00+00:00",
  "tour": {"path": "/tmp/preview-tour/index.md", "sha256": "<64 lowercase hex characters>"},
  "screenshots": [
    {"path": "/tmp/preview-tour/overview.png", "sha256": "<64 lowercase hex characters>"}
  ]
}
```

Use `"branch": ""` for the node's home. Each screenshot must be linked from the
tour with a local Markdown image reference. Paths are machine-local; keep the
receipt and captures outside the shared source repository.

Run `brain preview <node> --record <receipt.json>`, then `brain preview <node>
--json` to check it. The command verifies the configured bindings, source HEAD,
clean source status, capture timestamp and artifact digests. It stores the
receipt under `.brain/preview-evidence/` with private file permissions. It neither
opens the address nor captures images. Browser inspection, data checks and visual
review must actually happen before recording evidence.

`brain due` retains a preview-evidence item while a configured code preview lacks
matching evidence. Source changes, dirty source files, changed preview bindings,
missing screenshots or changed artifacts invalidate the receipt. Record a new
tour after checking the new preview; a failed recording preserves the old receipt.

## Own-job rendering and source receipts

For a job-scoped source-sample receipt, read the native skeleton:

```sh
brain preview-receipt-skeleton <node> --branch <job> --json
```

This is the supported consumer interface; product workers do not import core
Python or reconstruct generation hashes. See
[the output contract](verification-receipts.md#own-job-receipt-skeleton) for the
fields to fill before `brain preview-receipt`. The accessor writes nothing,
takes no instance lock, and leaves the node's preview and review URLs unchanged.
It does not render a page, create a screenshot tour, or establish readiness.
