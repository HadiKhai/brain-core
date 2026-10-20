# Dashboard development

`brain ui` serves the committed `dist/` with Python 3.9 and the standard library. Users need no Node installation. All JavaScript, CSS and fonts are local; the page has no external asset requests. `/api/state` and `/api/events` require the per-run key. `window.__STATE__` remains available for saved snapshots.

For development only:

```sh
cd ui
npm ci
npm run build
npm test
python3 tests/fixture.py --port 4109
```

Open `http://127.0.0.1:4109/?k=demo` for synthetic data, or `/snapshot` for a saved snapshot. Fixture scenarios include `?k=long`, `empty`, `partial`, `error`, and `loading`; no key demonstrates the permission state. No real brain is read. The ordinary Python CLI can also run against an isolated temporary HOME.

Evaluate `tests/ui-checks.js` (from the repository root) in the fixture browser to verify navigation, controls, view parity, filters, paging, command generation, escaping and shell geometry. Run it at desktop and narrow widths. The Python scenario suite verifies serving, key protection, live changes, static asset containment and the unchanged JSON contract.

The official shadcn CLI initialized Base UI / nova with Tailwind v4. Components under `src/components/ui/` are CLI-generated. Add components through that CLI; compose them in the page files. `shared.tsx` contains product layout compositions, not replacement primitives. One inset content area scrolls; the import action dock is a sibling footer. The bounded node sheet and mobile navigation sheet have their own scroll areas while modal.

License checks run during both build and tests. All lockfile dependencies must use an approved license; the Rollup build also checks the packages that enter the bundle and emits `assets/bundled-packages.json`, `assets/THIRD_PARTY_NOTICES.txt` (including license texts), and `assets/Geist-OFL.txt`. The font license ships beside the fonts. Missing licenses fail closed. Regenerate and commit the entire `dist/` after source changes; `npm ci && npm run build` must reproduce it.
