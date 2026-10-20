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

In another terminal, run `cd ui && npm run test:browser`. The runner uses one worker so browser scenarios run serially. If browser engines are missing, run `npx playwright install chromium webkit` first; these are development-only drivers.

Open `http://127.0.0.1:4109/?k=demo` for synthetic data, or `/snapshot` for a saved snapshot. Fixture scenarios include `?k=long`, `empty`, `single`, `ten`, `hundred`, `thousand`, `longtext`, `longtitle`, `rtl`, `partial`, `error`, and `loading`; no key demonstrates the permission state. No real brain is read. The ordinary Python CLI can also run against an isolated temporary HOME.

Evaluate `tests/ui-checks.js` (from the repository root) in the fixture browser to verify navigation, controls, view parity, filters, paging, command generation, escaping and shell geometry. Run it at desktop and narrow widths. `tests/ui-fitness.js` measures contrast, field labels, font size, rendered text/action insets, flush group headers and shared page geometry in each rendered view. Its mutation checks remove a content inset and shorten a group header, and require the audit to reject both. The Playwright tests add actual touch emulation, keyboard navigation, snapshots and state/error checks, plus live detail-panel interaction in Chromium and WebKit. They also test all six destinations and project detail at four sizes and both themes with empty, single-item, ten-item, hundred-item, thousand-request, long-text, long-title and RTL fixtures. Set `UI_TOUR_DIR` to an external report folder to save these synthetic screenshots and measurement JSON. Automatic failure screenshots and traces are disabled in that runner; explicit synthetic tour captures use UI_TOUR_DIR, and the T3 browser supplies the main tour and walkthrough.

The Python scenario suite verifies serving, key protection, live changes, static asset containment and the unchanged JSON contract.

The official shadcn CLI initialized Base UI / nova with Tailwind v4. Components under `src/components/ui/` are CLI-generated. Add components through that CLI; compose them in the page files. `shared.tsx` contains product layout compositions, not replacement primitives. One inset content area scrolls; the import action dock is a sibling footer. The bounded node sheet and mobile navigation sheet have their own scroll areas while modal.

License checks run during both build and tests. All lockfile dependencies must use an approved license; the Rollup build also checks the packages that enter the bundle and emits `assets/bundled-packages.json`, `assets/THIRD_PARTY_NOTICES.txt` (including license texts), and `assets/Geist-OFL.txt`. The font license ships beside the fonts. Missing licenses fail closed. Regenerate and commit the entire `dist/` after source changes; `npm ci && npm run build` must reproduce it.

The notebook identity uses copper actions, warm paper surfaces, a project outline and a dated work log. Every main destination has a primary task in the page header. Requests open the relevant conversation; proposals prepare terminal decisions with explicit copy feedback. The dashboard remains read-only. Long text opens a bounded reader, lists are paged or height-capped, and technical paths and identifiers are behind explicit details controls.
