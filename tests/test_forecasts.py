"""Advisory job forecasts, using fabricated ledgers and Git repositories only."""
import json
import os
from pathlib import Path
from unittest import mock

import core_loader
from scenario import Scenario, CORE


class JobForecasts(Scenario):
    def setUp(self):
        super().setUp()
        self.patch_env = mock.patch.dict(os.environ, self.env, clear=True)
        self.patch_env.start()
        self.addCleanup(self.patch_env.stop)
        self.m = core_loader.SourceFileLoader("forecast_core", str(CORE / "bin/brain")).load_module()
        self.root = self.init("sample")
        self.inst = self.m.Instance(self.root)
        self.n = self.inst.node("root")

    def rows(self, count=5):
        return [{"Job": f"fix/sample-{i}", "Started": f"2020-01-0{i + 1}T00:00Z",
                 "Ended": f"2020-01-0{i + 1}T02:00Z", "Outcome": "merged", "Hours": str(i + 1),
                 "Reviews": "1", "Class": "build", "Model": "sample-model", "Runtime": "codex",
                 "Files": str(i + 1), "Lines": str(100 * (i + 1)), "Usage": "host: fixture",
                 "Active h": str((i + 1) / 2), "Fresh in": str(5100000 * (i + 1))}
                for i in range(count)]

    def estimate(self, rows=None, kind="build", runtime="codex", size=None):
        return self.m.estimate_job(self.inst, self.n, self.rows() if rows is None else rows, kind, runtime, size)

    def test_five_builds_have_medians_and_spreads_and_two_are_sparse(self):
        f = self.estimate()
        self.assertEqual(f["samples"], 5)
        self.assertEqual(f["wall_h"], {"median": 3, "min": 1, "max": 5})
        self.assertEqual(f["active_h"], {"median": 1.5, "min": .5, "max": 2.5})
        self.assertEqual(f["tokens"], {"median": 15300000, "min": 5100000, "max": 25500000})
        self.assertEqual(f["weekly_percent"], {"median": .3, "min": .1, "max": .5})
        line = self.m.estimate_line(f)
        for text in ("wall 3 h", "active 1.5 h", "15.3M", "0.3%", "51M", "1.1M", "never a limit or gate"):
            self.assertIn(text, line)
        sparse = self.estimate(self.rows(2))
        self.assertNotIn("wall_h", sparse)
        self.assertIn("too little data", self.m.estimate_line(sparse))
        claude = [{**r, "Runtime": "claude"} for r in self.rows()]
        self.assertIn("68M", self.m.estimate_line(self.estimate(claude, runtime="claude")))

    def test_explicit_class_wins_and_nearest_sizes_exclude_other_classes_and_outcomes(self):
        rows = self.rows()
        rows += [{**rows[0], "Job": "build/review", "Class": "review", "Hours": "900"},
                 {**rows[0], "Job": "build/open", "Ended": "", "Hours": "900"},
                 {**rows[0], "Job": "build/abandoned", "Outcome": "abandoned", "Hours": "900"},
                 {**rows[0], "Job": "build/invalid", "Hours": "nan"}]
        rows += [{**rows[0], "Job": f"build/huge-{i}", "Files": "1000", "Lines": "100000", "Hours": "800"}
                 for i in range(4)]
        self.assertEqual(self.estimate(rows)["samples"], 9)
        self.assertEqual(self.m.job_kind(rows[0]["Job"], rows), "build")
        f = self.estimate(rows, size={"files": 3, "lines": 300})
        self.assertEqual(f["wall_h"]["max"], 5)
        self.assertEqual(f["selection"], "nearest 5 sizes")
        for row in rows:
            row.pop("Files", None)
        self.assertEqual(self.estimate(rows, size={"files": 3, "lines": 300})["samples"], 0)
        old = [{**r, "Class": ""} for r in self.rows()]
        self.assertEqual(self.estimate(old, kind="fix")["samples"], 5)
        self.assertEqual({f["kind"]: f for f in self.m.job_forecasts(self.rows())}["build"]["samples"], 5)

    def test_unmeasured_is_unknown_and_conversion_uses_three_attributed_pairs(self):
        rows = self.rows()
        for row in rows:
            row["Usage"] = "none found"
            row["Fresh in"] = "0"
        f = self.estimate(rows)
        self.assertNotIn("tokens", f)
        self.assertIn("too little data (0 measured", self.m.estimate_line(f))
        self.n["fm"]["forecast_processed_per_percent_codex"] = "10000000"
        self.assertEqual(self.estimate()["weekly_percent"]["median"], 1.53)
        pairs = self.rows(3)
        for row in pairs:
            row["Meter %"] = "1"
        c = self.estimate(pairs)["conversion"]
        self.assertEqual(c["source"], "ledger")
        self.assertEqual(c["processed_per_percent"], 10200000)
        self.assertEqual(self.estimate(pairs[:2])["conversion"]["source"], "configured/default")
        pairs[0]["Meter %"] = "nan"
        self.assertEqual(self.estimate(pairs)["conversion"]["source"], "configured/default")
        self.assertEqual(self.estimate(runtime="claude")["conversion"]["source"], "configured/default")
        self.n["fm"]["forecast_processed_per_percent_codex"] = "nan"
        self.assertEqual(self.estimate()["conversion"]["processed_per_percent"], 51000000)

    def test_brief_records_estimate_and_handback_keeps_size_and_actual_until_close(self):
        home = self.repo(self.tmp / "app")
        self.b(self.root, "new", "app", "--title", "App", "--home", str(home))
        self.b(self.root, "set", "app", "runtime=codex", "verify=true")
        self.b(self.root, "wt", "add", "app", "job/sample")
        inst = self.m.Instance(self.root)
        n = inst.node("app")
        row = self.m.open_job(self.m.read_jobs(n), "job/sample")
        self.m.write_jobs(inst, n, self.rows() + [row])
        contract = self.m.contract_file(n, row)
        contract.parent.mkdir(exist_ok=True)
        contract.write_text("## Size\n3 files, 300 lines\n")
        brief = self.b(self.root, "brief", "app", "--branch", "job/sample", "--class", "build", "--model", "sample-model")
        self.assertIn("Forecast build: 5 similar merged jobs (nearest 5 sizes)", brief)
        row = self.m.open_job(self.m.read_jobs(n), "job/sample")
        saved = row["Forecast"]
        self.assertEqual((row["Class"], row["Model"]), ("build", "sample-model"))
        wt = Path(n["local"]["worktrees"]["job/sample"]["path"])
        (wt / "new\nfile.txt").write_text("one\ntwo\n")
        (wt / "binary.bin").write_bytes(b"a\x00b")
        self.git("add", ".", cwd=wt)
        self.git("commit", "-qm", "fixture", cwd=wt)
        (wt / "README.md").write_text("uncommitted lines do not count\n")
        size = self.m.job_size(n, row)
        self.assertEqual((size["files"], size["lines"], size["binary_files"]), (2, 2, 1))
        self.git("restore", "README.md", cwd=wt)
        self.b(self.root, "jobs", "app", "--branch", "job/sample", "--usage-file", str(self.usage_file()))
        self.b(self.root, "handback", "app", "--branch", "job/sample", "--state", "done", "--summary", "Fixture result", "--reviews", "1",
               "--lessons", "none: fixture", "--report", "reports/fixture.md")
        hb = self.m.read_handback(n, row)
        self.assertGreaterEqual(hb["verify_duration"]["elapsed_seconds"], 0)
        self.assertFalse(hb["verify_duration"]["reused"])
        self.assertEqual(hb["estimate"], json.loads(saved))
        self.assertEqual(hb["actual"]["tokens"], 10200000)
        self.assertEqual(hb["actual"]["active_h"], 1)
        self.assertEqual(hb["actual"]["weekly_percent"], .2)
        self.assertEqual(hb["actual"]["size"]["lines"], 2)
        output = self.b(self.root, "jobs", "app", "--branch", "job/sample")
        self.assertIn("Actual at hand-back/close", output)
        self.assertIn("Git size 2 files, 2", output)
        latest = self.usage_file()
        latest.write_text(json.dumps({"host": "fixture", "fresh": 15300000, "active_minutes": 90}))
        self.b(self.root, "jobs", "app", "--branch", "job/sample", "--usage-file", str(latest))
        # Next coordinator-owned ledger write consumes the worker record; the worker never edits it.
        self.m.job_ended(inst, n, "job/sample", "merged", worktree=wt)
        closed = self.m.read_jobs(n)[-1]
        self.assertEqual((closed["Files"], closed["Lines"], closed["Forecast"]), ("2", "2", saved))
        self.assertEqual(json.loads(closed["Actual"])["tokens"], 15300000)
        self.assertIn("Actual at hand-back/close", self.b(self.root, "jobs", "app", "--branch", "job/sample"))

    def usage_file(self):
        path = self.tmp / "usage.json"
        path.write_text(json.dumps({"host": "fixture", "fresh": 10200000, "active_minutes": 60}))
        return path

    def test_failed_close_preserves_known_snapshot_and_unknown_never_becomes_zero(self):
        row = self.rows(1)[0]
        row["Forecast"] = json.dumps(self.estimate())
        actual = self.m.actual_cost(self.inst, self.n, row, self.m.row_usage(row))
        hb = {"version": 2, "job": row["Job"], "started": row["Started"], "state": "done",
              "lessons": "none: fixture", "actual": actual}
        path = self.m.handback_file(self.n, row)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(hb))
        for source in ("none found", "not counted", "not counted: OSError"):
            row["Usage"] = source
            unknown = self.m.actual_cost(self.inst, self.n, row, self.m.row_usage(row))
            self.assertIsNone(unknown["tokens"])
            self.assertIsNone(unknown["active_h"])
            self.assertIsNone(unknown["weekly_percent"])
            self.m.write_jobs(self.inst, self.n, [row])
            saved = json.loads(self.m.read_jobs(self.n)[0]["Actual"])
            self.assertEqual((saved["tokens"], saved["active_h"], saved["usage_at"]),
                             (actual["tokens"], actual["active_h"], actual["usage_at"]))
        self.assertIn("processed tokens 10.2M", self.m.actual_line({"version": 1, "tokens": 10200000}))
        self.assertNotIn("e+", self.m.actual_line({"version": 1, "wall_h": 125, "tokens": 10200000}))
        corrupt = {"version": 1, "tokens": "arbitrary", "unexpected": "arbitrary",
                   "size": {"files": "arbitrary", "lines": 1, "extra": "arbitrary"}}
        self.assertNotIn("arbitrary", self.m.actual_line(corrupt))
        self.assertNotIn("arbitrary", json.dumps(self.m.clean_actual(corrupt)))
        path.write_text(json.dumps({**hb, "actual": corrupt}))
        self.m.write_jobs(self.inst, self.n, [row])
        raw = self.m.read_jobs(self.n)[0]["Actual"]
        self.assertNotIn("arbitrary", raw)
        self.assertNotIn("unexpected", json.loads(raw))

    def test_runtime_metrics_and_host_calibration_input(self):
        claude = [{**r, "Runtime": "claude", "Fresh in": "68000000"} for r in self.rows()]
        f = self.estimate(claude)
        self.assertEqual(f["samples"], 5)
        self.assertEqual(f["measured"], 0)
        self.assertNotIn("weekly_percent", f)
        f = self.estimate(claude + self.rows(3))
        self.assertEqual(f["tokens"]["median"], 10200000)
        self.assertEqual(f["measured"], 3)
        path = self.usage_file()
        for value in (-1, 101, True, "nan"):
            path.write_text(json.dumps({"fresh": 10200000, "meter_percent": value}))
            with self.assertRaises(SystemExit):
                self.m.host_usage_cells(path, self.n)
        path.write_text(json.dumps({"host": "fixture", "fresh": 10200000, "meter_percent": 1}))
        cells = self.m.host_usage_cells(path, self.n)
        self.assertEqual(cells["Meter %"], "1.0")
        rows = [{**row, **cells} for row in self.rows(3)]
        self.assertEqual(self.estimate(rows)["conversion"]["processed_per_percent"], 10200000)
        path.write_text(json.dumps({"host": "fixture", "fresh": 20400000}))
        self.assertEqual(self.m.host_usage_cells(path, self.n)["Meter %"], "")
        # Existing jobs command is the supported calibration write path.
        row = {**self.rows(1)[0], "Ended": "", "Outcome": ""}
        self.m.write_jobs(self.inst, self.n, [row])
        path.write_text(json.dumps({"host": "fixture", "fresh": 10200000, "meter_percent": 1}))
        self.b(self.root, "jobs", "root", "--branch", row["Job"], "--usage-file", str(path))
        self.assertEqual(self.m.read_jobs(self.n)[0]["Meter %"], "1.0")
        tiny = [{**row, "Ended": "2020-01-01T02:00Z", "Outcome": "merged", "Meter %": "1e-320"}
                for row in self.rows(3)]
        self.assertEqual(self.estimate(tiny)["conversion"]["source"], "configured/default")

    def test_legacy_columns_round_trip_and_empty_brief_is_advisory(self):
        path = self.n["dir"] / "job-ledger.md"
        path.write_text("<!-- brain:job-ledger v1 -->\n| Job | Started | Ended | Extra |\n"
                        "| --- | --- | --- | --- |\n| old | 2020-01-01T00:00Z |  | keep |\n")
        rows = self.m.read_jobs(self.n)
        self.m.write_jobs(self.inst, self.n, rows)
        self.assertEqual(self.m.read_jobs(self.n)[0]["Extra"], "keep")
        brief = self.b(self.root, "brief", "root", "--class", "docs")
        self.assertIn("Forecast docs: 0 similar merged jobs", brief)
        self.assertIn("too little data", brief)
        self.assertIn("never a limit or gate", brief)
        self.assertIsNone(self.m.saved_forecast('{"version": 1}'))

    def test_size_uses_rebased_contribution_and_forecast_resumes_are_stable(self):
        home = self.repo(self.tmp / "app")
        self.b(self.root, "new", "app", "--title", "App", "--home", str(home))
        self.b(self.root, "set", "app", "runtime=codex")
        self.b(self.root, "wt", "add", "app", "job/sample")
        n = self.m.Instance(self.root).node("app")
        row = self.m.open_job(self.m.read_jobs(n), "job/sample")
        wt = Path(n["local"]["worktrees"]["job/sample"]["path"])
        (wt / "change").write_text("job\n")
        self.git("add", ".", cwd=wt)
        self.git("commit", "-qm", "job", cwd=wt)
        (home / "upstream").write_text("main\nmain\nmain\n")
        self.git("add", ".", cwd=home)
        self.git("commit", "-qm", "upstream", cwd=home)
        self.git("rebase", "main", cwd=wt)
        size = self.m.job_size(n, row)
        self.assertEqual((size["files"], size["lines"]), (1, 1))
        first_brief = self.b(self.root, "brief", "app", "--branch", "job/sample", "--class", "build")
        self.assertIn("runtime: codex", first_brief)
        first = self.m.open_job(self.m.read_jobs(n), "job/sample")["Forecast"]
        self.m.write_jobs(self.inst, n, self.rows() + self.m.read_jobs(n))
        self.assertEqual(len(self.m.read_jobs(n)), 6)
        next_brief = self.b(self.root, "brief", "app", "--branch", "job/sample", "--class", "build")
        self.assertEqual(len(self.m.read_jobs(n)), 6)
        self.assertEqual(next(s for s in first_brief.splitlines() if s.startswith("Forecast")),
                         next(s for s in next_brief.splitlines() if s.startswith("Forecast")))
        self.assertEqual(self.m.open_job(self.m.read_jobs(n), "job/sample")["Forecast"], first)
        switched = self.b(self.root, "brief", "app", "--branch", "job/sample", "--class", "build", "--runtime", "claude")
        changed = json.loads(self.m.open_job(self.m.read_jobs(n), "job/sample")["Forecast"])
        self.assertEqual((changed["class"], changed["runtime"]), ("build", "claude"))
        self.assertEqual(changed["samples"], 5)
        self.assertEqual(changed["conversion"]["processed_per_percent"], 68000000)
        self.assertIn("0 measured for claude", switched)

    def test_model_conditioning_excludes_other_models_and_invalidates_saved_route(self):
        rows = self.rows(3)
        other = [{**r, 'Job': r['Job'] + '-other', 'Model': 'other-model', 'Hours': '90',
                  'Fresh in': '900000000'} for r in rows]
        f = self.m.estimate_job(self.inst, self.n, rows + other, 'build', 'codex', model='sample-model')
        self.assertEqual(f['wall_h']['max'], 3)
        self.assertEqual(f['tokens']['max'], 15300000)
        self.assertEqual(f['model'], 'sample-model')
        self.assertIn('sample-model', self.m.estimate_line(f))
        sparse = self.m.estimate_job(self.inst, self.n, rows[:2] + other, 'build', 'codex', model='sample-model')
        self.assertFalse(sparse['enough_data'])
        self.assertNotIn('tokens', sparse)

    def test_hire_and_brief_share_saved_forecast_and_model_switch_recomputes(self):
        home = self.repo(self.tmp / 'app')
        self.b(self.root, 'new', 'app', '--title', 'App', '--home', str(home))
        self.b(self.root, 'set', 'app', 'runtime=codex')
        self.b(self.root, 'specialist', 'new', 'tester', '--title', 'Tester')
        self.b(self.root, 'wt', 'add', 'app', 'job/sample')
        inst = self.m.Instance(self.root)
        n = inst.node('app')
        row = self.m.open_job(self.m.read_jobs(n), 'job/sample')
        self.m.write_jobs(inst, n, self.rows() + [row])
        args = ('--branch', 'job/sample', '--class', 'build', '--model', 'sample-model')
        hired = self.b(self.root, 'hire', 'tester', 'app', *args, '--task', 'Test fixture')
        estimate = next(line for line in hired.splitlines() if line.startswith('Forecast'))
        self.assertIn('5 similar merged jobs', estimate)
        saved = self.m.open_job(self.m.read_jobs(n), 'job/sample')['Forecast']
        self.assertEqual(json.loads(saved)['model'], 'sample-model')
        brief = self.b(self.root, 'brief', 'app', *args)
        self.assertIn(estimate, brief.splitlines())
        self.assertEqual(self.m.open_job(self.m.read_jobs(n), 'job/sample')['Forecast'], saved)
        switched = self.b(self.root, 'hire', 'tester', 'app', '--branch', 'job/sample',
                          '--class', 'build', '--model', 'other-model', '--task', 'Test fixture')
        self.assertIn('0 similar merged jobs', switched)
        self.assertEqual(json.loads(self.m.open_job(self.m.read_jobs(n), 'job/sample')['Forecast'])['model'], 'other-model')
        no_branch = self.b(self.root, 'hire', 'tester', 'app', '--class', 'docs', '--task', 'Read fixture')
        self.assertIn('Forecast docs:', no_branch)

    def test_verify_duration_records_new_time_reuse_and_configured_slow_advice(self):
        row = self.rows(1)[0]
        fresh = self.m.record_verify_duration(self.inst, self.n, row, {'verify_reused': False}, 601.25)
        self.assertEqual(fresh['elapsed_seconds'], 601.25)
        self.assertTrue(fresh['slow'])
        reused = self.m.record_verify_duration(self.inst, self.n, row, {'verify_reused': True}, 900)
        self.assertEqual(reused['elapsed_seconds'], 0)
        self.assertFalse(reused['slow'])
        self.n['fm']['verify_warn_seconds'] = '900'
        self.assertFalse(self.m.record_verify_duration(self.inst, self.n, row, {}, 601.25)['slow'])
        for bad in (-1, float('nan'), True):
            self.assertIsNone(self.m.record_verify_duration(self.inst, self.n, row, {}, bad))
        hb = {'version': 2, 'job': row['Job'], 'started': row['Started'], 'state': 'done',
              'lessons': 'none: fixture', 'verify_duration': fresh}
        path = self.m.handback_file(self.n, row)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(hb))
        self.m.write_jobs(self.inst, self.n, [row])
        saved = self.m.read_jobs(self.n)[0]
        self.assertEqual(saved['Verify s'], '601.25')
        self.assertEqual(saved['Verify reused'], 'false')
        self.n['fm']['verify_warn_seconds'] = '600'
        self.assertIn('slow verify', '\n'.join(self.m.job_advice(self.inst, self.n, row)))
        path.write_text(json.dumps({**hb, 'verify_duration': {'elapsed_seconds': 'private', 'reused': False}}))
        self.m.write_jobs(self.inst, self.n, [row])
        self.assertNotIn('private', self.m.read_jobs(self.n)[0]['Verify s'])
