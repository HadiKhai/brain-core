# Dashboard development

`brain ui` serves the committed `dist/` with Python 3.9 and the standard library. Users need no Node installation. All JavaScript, CSS and fonts are local; the page has no external asset requests. `/api/state`, `/api/events` and `/api/search` require the per-run key. `window.__STATE__` remains available for saved snapshots; pickers then rank locally with the same rules as the server (`src/rank.ts` mirrors `ui_search` in `bin/brain`).

The look is set by [IDENTITY.md](IDENTITY.md): a calm control room, dark first, ember for "needs you" and the primary action, signal green for live, Geist and Geist Mono, dense rows.

## Structure

The sidebar holds the hierarchy; a page holds one topic and at most three sections.

- **Overview** (a decision dashboard): KPIs with real trends, one mark per open thread, the 24-hour pulse strip, subscription and machine meters, the top five items that need you (tabs: requests, coordinator steps, proposals) and the threads moving now with a graph preview. It stays within two screens.
- **Inbox**: every request and coordinator step, each with its action.
- **Brain** tree: All projects, then each project (live dot, needs-you count) with its coordinator link and live threads. A project's page has a KPI strip, a Work section (needs you, threads, jobs, activity) and a Notes section (status sections, decisions, limits, reports, about). The `+` beside the group opens **Add projects**, the sheet that replaced the import checklist page.
- **Agents**: Specialists, Jobs (the full ledger), Threads, Usage.
- **Learnings** and **Graph** (a dependency-free force layout in `src/graph-layout.ts`; labels never overlap; search glows matches, recent activity pulses).

Old addresses still land: `#view=agents` opens Jobs, `#view=imports` opens Add projects over Overview, and `node=<id>` on any view opens that project's page.

Every field that points at another record uses `RecordPicker` (`src/picker.tsx`): the list opens on focus with the most relevant records, typing searches `/api/search` (debounced, stale-safe), more rows load on scroll, the match is highlighted and a secondary line tells similar names apart. The server ranks exact, then name start, then a later word's start, then earlier matches, then any match; accent-, case- and Arabic-letter-form-insensitive; ties A to Z.

## Build and test

For development only:

```sh
cd ui
npm ci
npm run build
npm test
python3 tests/fixture.py --port 4109
```

In another terminal, run `cd ui && npm run test:browser`. The runner uses one worker so browser scenarios run serially. If browser engines are missing, run `npx playwright install chromium webkit` first; these are development-only drivers.

Open `http://127.0.0.1:4109/?k=demo` for synthetic data, or `/snapshot` for a saved snapshot. Fixture scenarios include `?k=long`, `empty`, `single`, `ten`, `hundred`, `thousand`, `longtext`, `longtitle`, `rtl`, `partial`, `error`, `loading`, `slow` (a slow search endpoint) and `many` (1,200 made-up projects behind the picker's search); no key demonstrates the permission state. No real brain is read. `UI_DIST=<folder>` serves another build, for example a previous one, to record a gate's failure.

- `tests/ui-fitness.spec.ts` is the **ui-fitness gate**: one check per lean-structure rule (one title, no option overflow, themed scrollbars, sidebar hierarchy, no page without a job, at most two surface levels and no single-item box or short inner scroll, lean rows, scroll chaining, Overview as a dashboard), over every sidebar destination. The rules live in `tests/fitness-rules.ts`; `tests/ui-fitness.mutations.spec.ts` breaks each rule on purpose and requires the check to fail, then pass again. Set `UI_FITNESS_OUT` to save the findings as JSON.
- `tests/picker.spec.ts` covers the picker's checklist: empty focus, one letter, a later word, a mid-word query, no match, 1,000+ records with scrolling and a slow network.
- `tests/pagination.spec.ts` checks tab and route changes, live-data shrink and regrowth across all six paged-list implementations, and retained page-size choices. Lists share `src/hooks/use-page.ts` so the displayed range and rendered rows use the same clamped page.
- `tests/interaction.spec.ts` adds touch, keyboard, snapshots, states and the Chromium and WebKit reader scenarios; `tests/alignment.spec.ts` audits every destination at four sizes and both themes with `../tests/ui-fitness.js` (contrast, labels, field font size, section insets, phone gutters); `tests/extremes.spec.ts` renders every destination with seeded empty, single, ten, hundred, thousand, long-text, long-title and RTL data. Set `UI_TOUR_DIR` to save these synthetic screenshots.

The Python scenario suite verifies serving, key protection, the search endpoint, live changes, static asset containment and the JSON contract (fields are only ever added). `tests/test_ui_idle.py` also proves that unchanged reads reuse their snapshot, real input changes invalidate it, sampler/cache output does not trigger rebuilds, and viewing spend never writes transcript aggregates. `tests/live-idle.spec.ts` verifies the client stops streaming after five seconds without input, checks once a minute while idle, pauses hidden-tab requests unless notifications are enabled, and reconnects on activity. Opted-in notifications retain one hidden-tab check each minute. The server sleeps with no clients. Metadata checks follow the active stream cadence or the idle minute poll; spend progresses in bounded in-memory slices. Node and worktree display caches invalidate on their own record, file, ref, packet and declared verify-input metadata. Integrity counts batch changes at most once every five minutes while observed; `brain check` remains immediate. Time-based due/readiness decisions refresh at five-minute boundaries.

## Previewing a branch

`brain` on PATH normally re-runs itself with the brain's own core. A core **worktree** (a checkout whose `.git` is a file) serves its own build instead, so a dashboard in development can be previewed against a real brain without installing it:

```sh
BRAIN_NO_REEXEC=1 python3 <worktree>/bin/brain -b <brain> ui --port <port>
```

The server sleeps without observers and reuses its snapshot until source metadata changes. Recently observed tabs retain a short lease so unfinished spend can progress between their minute checks. Brain records and Git refs are checked at the live cadence; repository working files are checked at most every 15 seconds. Machine meters update separately; age-dependent decisions and rolling spend windows are rechecked every five minutes while observed. Rehearsal display readiness also follows the installed core identity. Spend aggregation stays in memory when serving the dashboard and continues in bounded slices while a tab is observing, even if source files stop changing. Transcript changes update spend separately from the main snapshot.

The first output line names the serving core and its commit (`brain ui core: <path> @ <commit>`); the second is the keyed address. Look at real data only through a copy of the brain (`cp -R`), never by changing the original.

## Components and licenses

The official shadcn CLI initialized Base UI / nova with Tailwind v4. Components under `src/components/ui/` are CLI-generated. Add components through that CLI; compose them in the page files. `shared.tsx` and `dash.tsx` contain product layout compositions (page header, KPI tile, segmented bar, sparkline, section tabs), not replacement primitives. One inset content area scrolls and chains to nothing; readers and sheets have their own bounded bodies while modal.

License checks run during both build and tests. All lockfile dependencies must use an approved license; the Rollup build also checks the packages that enter the bundle and emits `assets/bundled-packages.json`, `assets/THIRD_PARTY_NOTICES.txt` (including license texts), `assets/Geist-OFL.txt` and `assets/GeistMono-OFL.txt`. The OFL is accepted only for those two font packages. Missing licenses fail closed. Regenerate and commit the entire `dist/` after source changes; `npm ci && npm run build` must reproduce it.
