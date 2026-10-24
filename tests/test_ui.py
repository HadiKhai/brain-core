"""Scenario tests for core/bin/brain. Run with `brain selftest` (or python3 -m unittest in tests/).

Every test builds instances in a throwaway folder that is also $HOME, so nothing real is touched.
"""
import json
import re
import datetime
import importlib.util
import core_loader
import threading
from unittest import mock
import argparse
import contextlib
import io
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

CORE = Path(os.environ.get("BRAIN_CORE") or Path(__file__).resolve().parent.parent)


from scenario import Scenario, AutonomyProject, shlex_quote


class LiveView(Scenario):
    def test_unavailable_live_runtime_stops_new_calls_and_keeps_a_failed_gate(self):
        import contextlib
        import importlib.util
        import io
        from unittest import mock
        spec = importlib.util.spec_from_file_location("brain_live_under_test", CORE / "tests" / "live.py")
        live = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(live)
        calls = []
        def unavailable(run, *args, **kwargs):
            calls.append(run.scenario.__name__)
            raise live.RuntimeUnavailable("provider session limit")
        def first(run):
            run.agent(self.tmp, "test")
        def second(run):
            run.agent(self.tmp, "test")
        mkdtemp = tempfile.mkdtemp
        def temporary(*args, **kwargs):
            kwargs.setdefault("dir", self.tmp)
            return mkdtemp(*args, **kwargs)
        output = io.StringIO()
        with mock.patch.object(live.Run, "agent", unavailable), \
             mock.patch.object(live, "SCENARIOS", [first, second]), \
             mock.patch.object(live.tempfile, "mkdtemp", temporary), \
             mock.patch.object(sys, "argv", ["live.py", "--runtimes", "codex", "--jobs", "1"]), \
             mock.patch.dict(os.environ, {"LIVE_CODEX_BIN": sys.executable}), \
             contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as ended:
            live.main()
        self.assertEqual(ended.exception.code, 1)
        self.assertEqual(calls, ["first"])
        self.assertIn("BLOCKED", output.getvalue())
        self.assertIn("not started: codex unavailable", output.getvalue())

    def test_ui_serves_a_read_only_live_state_to_this_machine_only(self):
        import socket
        import urllib.error
        import urllib.request
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        st = r / "nodes" / "app" / "status.md"
        st.write_text(st.read_text().replace("| --- | --- | --- | --- |\n",
                      "| --- | --- | --- | --- |\n| job-a | [job a](t3-thread://v1/t1) (Codex) | - | building |\n")
                      .replace("## Blocked / needs the operator\n", "## Blocked / needs the operator\n\n- The API key\n"))
        self.git("commit", "-qam", "app: job a started", cwd=r)
        self.b(r, "wt", "add", "app", "job-a")
        port = json.loads(self.b(r, "ui", "--json"))   # the same state, once
        app = next(n for n in port["nodes"] if n["id"] == "app")
        self.assertEqual(app["work"][0]["link"], "t3-thread://v1/t1")
        self.assertEqual(app["work"][0]["runtime"], "Codex")
        self.assertEqual(app["worktrees"][0]["branch"], "job-a")
        need = next(i for i in port["attention"] if i["node"] == "app" and i["kind"] == "blocked")
        self.assertEqual(need["text"], "The API key")
        self.assertFalse(need["escalated"])
        self.assertIn("age_hours", need)
        self.assertGreaterEqual(sum(port["activity"]["counts"].get("app", [])), 1)   # "app: …" commits count for app
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            free = sk.getsockname()[1]
        p = subprocess.Popen([sys.executable, str(r / "core" / "bin" / "brain"), "ui", "--port", str(free),
                              "--every", "0.5"], cwd=r, env=self.env, stdout=subprocess.PIPE, text=True)
        try:
            core = p.stdout.readline()   # the first line names the core that serves the page, and its commit
            self.assertTrue(core.startswith("brain ui core: "), core)
            self.assertIn(" @ ", core)
            line = p.stdout.readline()
            full = line.split(": ", 1)[1].split()[0]
            self.assertTrue(full.startswith("http://127.0.0.1:"))
            url, key = full.split("?k=")
            page = urllib.request.urlopen(url, timeout=5).read()
            self.assertIn(b"<title>", page)
            self.assertIn(b'<div id="root">', page)
            self.assertNotIn(b"The API key", page)  # public shell contains no brain snapshot
            assets = re.findall(rb'(?:src|href)="(\./assets/[^"?]+)"', page)
            self.assertGreaterEqual(len(assets), 2)
            for asset in assets:
                response = urllib.request.urlopen(url + asset.decode()[2:], timeout=5)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertGreater(len(response.read()), 0)
            # Runtime notices and font license ship alongside the static assets.
            notices = urllib.request.urlopen(url + "assets/THIRD_PARTY_NOTICES.txt", timeout=5).read()
            self.assertIn(b"react ", notices)
            self.assertIn(b"SIL OPEN FONT LICENSE", urllib.request.urlopen(url + "assets/Geist-OFL.txt", timeout=5).read())
            # A rebuild while serving: the shell follows the files on disk, never stale asset names.
            shell = r / "core/ui/dist/index.html"
            rebuilt = page.replace(b"<title>", b"<title>Rebuilt ")
            shell.write_bytes(rebuilt)
            self.assertEqual(urllib.request.urlopen(url, timeout=5).read(), rebuilt)
            shell.write_bytes(page)
            script = next(a for a in assets if a.endswith(b".js"))
            self.assertIn(b"__STATE__", urllib.request.urlopen(url + script.decode()[2:], timeout=5).read())
            for path in ("assets/../index.html", "assets/%2e%2e%2findex.html", "assets/%2e%2e%5cindex.html", "assets/.hidden", "assets/nope.js", "assets/%00.js", "assets/a%00b", "assets/%0A.js", "assets/%FF.js"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(url + path, timeout=5)
                self.assertEqual(e.exception.code, 404)
            outside = self.tmp / "private.txt"
            outside.write_text("must not be served")
            (r / "core/ui/dist/assets/outside.txt").symlink_to(outside)
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(url + "assets/outside.txt", timeout=5)
            self.assertEqual(e.exception.code, 404)
            for endpoint in ("api/events", "api/events?k=wrong", "api/state?k=wrong"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(url + endpoint, timeout=5)
                self.assertEqual(e.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as e:   # another account on this machine has no key
                urllib.request.urlopen(url + "api/state", timeout=5)
            self.assertEqual(e.exception.code, 403)
            state = json.loads(urllib.request.urlopen(url + "api/state?k=" + key, timeout=5).read())
            self.assertEqual(state["brain"]["name"], "alice")
            found = json.loads(urllib.request.urlopen(url + "api/search?kind=node&q=ap&k=" + key, timeout=5).read())
            self.assertEqual([i["id"] for i in found["items"]], ["app"])          # the picker's server search
            for endpoint in ("api/search?q=ap", "api/search?q=ap&k=wrong"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(url + endpoint, timeout=5)
                self.assertEqual(e.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(url + "api/search?offset=x&k=" + key, timeout=5)
            self.assertEqual(e.exception.code, 400)
            self.assertEqual(set(state), set(port))  # --json contract and live snapshot agree
            first_snapshot = state

            req = urllib.request.Request(url + "api/state?k=" + key, headers={"Host": "evil.example"})
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(e.exception.code, 403)              # another site can't read it through a browser
            ev = urllib.request.urlopen(url + "api/events?k=" + key, timeout=10)
            first = ev.readline()
            self.assertTrue(first.startswith(b"data: {"))
            streamed = json.loads(first[len(b"data: "):])
            self.assertEqual(set(streamed), set(first_snapshot))
            self.assertEqual(streamed["brain"]["name"], first_snapshot["brain"]["name"])
            ev.readline()
            # a change in the files reaches an open page without a reload
            (r / "nodes" / "app" / "inbox.md").write_text("- 2026-01-01 00:00 from worker: question: which key?\n")
            deadline = time.time() + 10
            got = b""
            while time.time() < deadline and b"which key?" not in got:
                got = ev.readline()
            self.assertIn(b"which key?", got)
            ev.close()
        finally:
            p.terminate()
            p.wait(timeout=5)
            p.stdout.close()


class ExpandedUI(Scenario):
    def setUp(self):
        super().setUp()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.env["BRAIN_CONFIG"] = str(self.tmp / "config" / "brains.json")
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        loader = core_loader.SourceFileLoader("brain_ui_test", str(CORE / "bin/brain"))
        self.m = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.m)

    def first_paint_root(self):
        root = self.tmp / "alice"
        root.mkdir()
        (root / "instance.md").write_text("---\nname: alice\nprincipals: alice\ntimezone: UTC\n---\n")
        (root / "node.md").write_text("---\nid: root\ntitle: Alice workspace\nowners: alice\nhome: here\n---\n")
        (root / "status.md").write_text(
            "## Now\nA synthetic first reading.\n\n## Blocked\n"
            "- Waiting on operator; clears when choice arrives; since: 2026-01-01T00:00Z\n"
            "- Untimestamped decision; recommendation: proceed; default: wait\n"
            "- Invalid decision; since: not-a-date\n")
        child = root / 'nodes' / 'meadow'
        child.mkdir(parents=True)
        (child / 'node.md').write_text('---\nid: meadow\ntitle: Meadow\nowners: alice\n---\n')
        (child / 'status.md').write_text('## Blocked\n- Child decision\n- Child invalid; since: invalid\n')
        (root / '.brain').mkdir()
        (root / '.brain/local.json').write_text('{"machine":"demo","nodes":{}}')
        return root

    def test_first_paint_reads_real_nodes_without_waiting_for_subprocesses(self):
        root = self.first_paint_root()
        inst = self.m.UIInstance(root)
        start = time.monotonic()
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("slow subprocess layer")):
            state = self.m.ui_initial_state(inst)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(state["brain"]["name"], "alice")
        self.assertEqual(state["nodes"][0]["title"], inst.nodes()[0]["fm"]["title"])
        self.assertTrue(state["brain"]["filling_in"])

    def test_git_snapshot_reuses_only_reads_and_expires_after_each_build(self):
        owner = self.m._owners["git"]
        result = subprocess.CompletedProcess([], 0, "abc", "")
        with mock.patch.object(subprocess, "run", return_value=result) as run:
            with owner.git_read_snapshot():
                self.assertEqual(owner.git("rev-parse", "HEAD").stdout, "abc")
                self.assertEqual(owner.git("rev-parse", "HEAD").stdout, "abc")
                owner.git("update-ref", "refs/test", "abc")
                owner.git("update-ref", "refs/test", "abc")
                for query in (("remote",), ("symbolic-ref", "--short", "HEAD"),
                              ("-c", "diff.ignoreSubmodules=none", "status", "--porcelain")):
                    owner.git(*query)
                    owner.git(*query)
            self.assertEqual(run.call_count, 6)
            owner.git("rev-parse", "HEAD")
            self.assertEqual(run.call_count, 7)

    def test_snapshot_age_is_visible_until_a_successful_full_refresh(self):
        box = {"partial": True, "built_at": 100, "building": True, "error": ""}
        self.assertEqual(self.m.ui_loading(box, now=112),
                         {"filling_in": True, "age_seconds": 12, "refreshing": True, "failed": False, "version": 0})
        box.update(partial=False, building=False)
        self.assertFalse(self.m.ui_loading(box, now=120)["refreshing"])
        box.update(building=True)
        self.assertEqual(self.m.ui_loading(box, now=130)["age_seconds"], 30)
        box.update(building=False, error="private failure")
        status = self.m.ui_loading(box, now=140)
        self.assertTrue(status["failed"])
        self.assertNotIn("private", json.dumps(status))
        box.update(error='', request=2, checked=1)
        self.assertTrue(self.m.ui_loading(box, now=140)['refreshing'])

    def test_server_publishes_real_partial_state_then_the_unchanged_full_state(self):
        import http.server
        import urllib.request
        root = self.first_paint_root()
        inst = self.m.UIInstance(root)
        fixed = inst.now_dt()
        ready, entered, release = threading.Event(), threading.Event(), threading.Event()
        servers, errors = [], []
        real_server, real_run = http.server.ThreadingHTTPServer, subprocess.run
        def server(*args, **kwargs):
            value = real_server(*args, **kwargs)
            servers.append(value)
            ready.set()
            return value
        def slowed(args, *a, **kw):
            if args[0] == "git":
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("subprocess fixture was not released")
            return real_run(args, *a, **kw)
        def run():
            try:
                self.m.cmd_ui(argparse.Namespace(json=False, scan=[], open=False, write=False, port=0, every=0.05))
            except BaseException as error:
                errors.append(error)
        with mock.patch.object(self.m.UIInstance, "now_dt", return_value=fixed):
            expected = self.m.ui_state(inst, len(self.m.collect_problems(inst)[0]), machine_spend={})
            spend = mock.Mock(inst=inst, value={}, revision=0, permissions=None)
            spend.advance.return_value = False
            spend.refresh_view.return_value = False
            with mock.patch.object(self.m, "load", return_value=inst), \
                 mock.patch.object(self.m, "UISpend", return_value=spend), \
                 mock.patch.object(http.server, "ThreadingHTTPServer", side_effect=server), \
                 mock.patch.object(subprocess, "run", side_effect=slowed), \
                 contextlib.redirect_stdout(io.StringIO()):
                start = time.monotonic()
                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                stream = None
                try:
                    self.assertTrue(ready.wait(0.5), "server waited for the slow subprocess layer")
                    self.assertTrue(entered.wait(0.5))
                    port = servers[0].server_address[1]
                    record = json.loads((root / '.brain/ui-servers' / (str(port) + '.json')).read_text())
                    base = 'http://127.0.0.1:' + str(port)
                    def get(path):
                        return urllib.request.urlopen(base + path + '?k=' + record['key'], timeout=2)
                    with get('/api/state') as response:
                        self.assertEqual(response.headers['X-Brain-UI-Version'], '1')
                        first = json.load(response)
                    self.assertTrue(first['brain']['filling_in'])
                    self.assertEqual(first['nodes'][0]['title'], expected['nodes'][0]['title'])
                    self.assertLess(time.monotonic() - start, 1)
                    stream = get('/api/events')
                    initial = json.loads(stream.readline()[6:])
                    self.assertEqual(initial, first)
                    self.assertEqual(stream.readline(), b'id: 1\n')
                    with get('/api/loading') as response:
                        self.assertTrue(json.load(response)['refreshing'])
                    release.set()
                    full = initial
                    while full['brain'].get('filling_in'):
                        line = stream.readline()
                        if line.startswith(b'data: '):
                            full = json.loads(line[6:])
                    self.assertEqual(full, expected)
                    # Finish the initial pending metadata check before asserting it is settled.
                    with get('/api/state') as response:
                        self.assertEqual(json.load(response), expected)
                    with get('/api/loading') as response:
                        self.assertFalse(json.load(response)['refreshing'])
                finally:
                    release.set()
                    if stream:
                        stream.close()
                    if servers:
                        servers[0].shutdown()
                    thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])

    def test_open_reuses_matching_version_and_stops_only_an_identified_old_server(self):
        root = self.first_paint_root()
        inst = self.m.UIInstance(root)
        module = self.m._owners['ui_reuse_server']
        (root / 'lib/brain').mkdir(parents=True)
        source = root / 'lib/brain/ui.py'
        source.write_text('old source')
        with mock.patch.object(module.common, 'CORE', root):
            old = module.ui_code_version()
            source.write_text('new source')
            self.assertNotEqual(module.ui_code_version(), old)
        record = {'pid': 12345, 'root': str(root), 'version': 'current', 'write': False, 'key': 'demo-key'}
        module.ui_server_record(inst, 4747, record)
        identity = {k: record[k] for k in ('pid', 'root', 'version', 'write')}
        with mock.patch.object(module, 'ui_server_request', return_value=identity) as request, \
             mock.patch.object(os, 'kill') as kill:
            self.assertEqual(module.ui_reuse_server(inst, 4747, 'current'),
                             'http://127.0.0.1:4747/?k=demo-key')
            self.assertEqual(request.call_count, 1)
            self.assertIsNone(module.ui_reuse_server(inst, 4747, 'new'))
            self.assertEqual(request.call_args.args, (4747, 'demo-key'))
            self.assertEqual(request.call_args.kwargs, {'stop': True})
            kill.assert_not_called()
        result = subprocess.CompletedProcess([], 0, '12345\n', '')
        with mock.patch.object(module, 'ui_server_request', side_effect=OSError), \
             mock.patch.object(subprocess, 'run', return_value=result), \
             mock.patch.object(module, 'ui_legacy_owner', return_value=False), \
             mock.patch.object(os, 'kill') as kill:
            self.assertIsNone(module.ui_reuse_server(inst, 4747, 'new'))
            kill.assert_not_called()

    def test_legacy_server_ownership_requires_python_brain_ui_and_the_same_brain(self):
        # Cwd/argv cannot prove the selected instance: BRAIN_ROOT can override cwd,
        # and re-exec can consume the original CLI selector before ps observes it.
        module = self.m._owners['ui_legacy_owner']
        root = self.first_paint_root()
        child = root / 'nodes/meadow'
        variants = ['-b beta ui', 'ui -b beta', '--brain beta ui', 'ui --brain beta',
                    '--brain=beta ui', 'ui --brain=beta', '-b alice ui', 'ui',
                    'ui --port 4747', 'check']
        other = self.tmp / 'beta'
        other.mkdir()
        (other / 'instance.md').write_text('---\nname: beta\n---\n')
        (other / 'node.md').write_text('---\nid: root\n---\n')
        inst = self.m.UIInstance(root)
        for selection in variants:
            for cwd in (root, child):
                for env_root in ('', str(other)):
                    with self.subTest(selection=selection, cwd=cwd, env=env_root):
                        command = str(os.getuid()) + ' ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(CORE / 'bin/brain')) + ' ' + selection
                        def run(args, **kwargs):
                            output = command if args[0] == 'ps' else ('n' + str(cwd) + '\n' if '-d' in args else '12345\n')
                            return subprocess.CompletedProcess(args, 0, output, '')
                        with mock.patch.dict(os.environ, {'BRAIN_ROOT': env_root}), \
                             mock.patch.object(subprocess, 'run', side_effect=run), \
                             mock.patch.object(os, 'kill') as kill, \
                             mock.patch.object(module, 'ui_server_request', side_effect=OSError) as request:
                            self.assertIsNone(module.ui_reuse_server(inst, 4747, 'candidate'))
                            self.assertFalse(module.ui_legacy_owner(12345, root))
                            kill.assert_not_called()
                            request.assert_not_called()

    def test_partial_blocked_ages_are_unknown_without_history_and_do_not_poison_full_metadata(self):
        inst = self.m.UIInstance(self.first_paint_root())
        owner = self.m._owners['blocked_entries']
        history = subprocess.CompletedProcess([], 0, '2026-01-01T00:00:00+00:00\n', '')
        with mock.patch.object(owner._m_common, 'git', return_value=history) as git:
            partial = self.m.ui_initial_state(inst)
            git.assert_not_called()
            for node in partial['nodes']:
                unknown = [v for v in node['blocked_metadata'] if v.get('since') is None]
                self.assertEqual(len(unknown), 2)
                self.assertTrue(all(v['age_hours'] is None for v in unknown))
                self.assertTrue(all(not v['escalated'] for v in unknown))
            explicit = partial['nodes'][0]['blocked_metadata'][0]
            self.assertEqual(explicit['since'], '2026-01-01T00:00:00+00:00')
            self.assertTrue(explicit['waiting_on_operator'])
            self.assertEqual(partial['nodes'][0]['blocked_metadata'][1]['recommendation'], 'proceed')
            for node in inst.nodes():
                full = owner.blocked_entries(inst, node)
                self.assertTrue(all(v['since'] and v['age_hours'] is not None for v in full))
            self.assertEqual(git.call_count, 4)
            # A populated full cache must not turn a later file-only age into history.
            again = self.m.ui_initial_state(inst)
            self.assertIsNone(again['nodes'][1]['blocked_metadata'][0]['age_hours'])

    def test_open_does_not_fall_back_to_another_port_for_an_unrelated_listener(self):
        import http.server
        import webbrowser
        inst = self.m.UIInstance(self.first_paint_root())
        clock = iter(range(0, 100, 10))
        with mock.patch.object(self.m, 'load', return_value=inst), \
             mock.patch.object(self.m._owners['ui_reuse_server'], 'ui_reuse_server', return_value=None), \
             mock.patch.object(http.server, 'ThreadingHTTPServer', side_effect=OSError('busy')) as server, \
             mock.patch.object(webbrowser, 'open') as opened, \
             mock.patch.object(time, 'monotonic', side_effect=lambda: next(clock)), \
             self.assertRaisesRegex(SystemExit, 'port 4747 is busy'):
            self.m.cmd_ui(argparse.Namespace(json=False, scan=[], open=True, write=False, port=4747, every=1))
        self.assertEqual(server.call_count, 1)
        self.assertEqual(server.call_args.args[0], ('127.0.0.1', 4747))
        opened.assert_not_called()

    def test_ui_scan_matches_cli_and_does_not_write(self):
        root = self.init("alice")
        first = self.repo(self.tmp / "code" / "first")
        second = self.repo(self.tmp / "elsewhere" / "second")
        self.b(root, "new", "first", "--title", "First", "--home", str(first))
        expected = json.loads(self.b(root, "scan", str(first.parent), str(second.parent), "--json"))
        def files():
            return {str(p.relative_to(self.tmp)): p.read_bytes() for p in self.tmp.rglob("*")
                    if p.is_file() and not p.is_symlink()}
        before = files()
        state = json.loads(self.b(root, "ui", "--json", "--scan", str(first.parent),
                                  "--scan", str(second.parent)))
        self.assertEqual(state["scan"], expected)
        self.assertEqual(files(), before)
        self.assertEqual([c["adoptable"] for c in state["scan"]], [False, True])
        self.assertEqual(json.loads(self.b(root, "ui", "--json"))["scan"], [])
        self.assertIn("every scan path must be a directory", self.b(root, "ui", "--json", "--scan",
                                                                   str(self.tmp / "missing"), ok=False))

    def test_ui_exposes_due_and_complete_ledger_without_polling(self):
        root = self.init("alice")
        self.b(root, "new", "project", "--title", "Project")
        inst = self.m.Instance(root)
        node = inst.node("project")
        rows = [{"Job":f"job-{i}", "Started":"2026-01-01T00:00:00+00:00", "Ended":"2026-01-01T01:00:00+00:00",
                 "Hours":"1", "Reviews":"2", "Outcome":"merged", "Lesson check":"required"} for i in range(65)]
        self.m.write_jobs(inst, node, rows)
        with mock.patch.object(self.m, "usage_refresh", side_effect=AssertionError("UI must never poll")):
            state = self.m.ui_state(inst)
        ui_node = next(n for n in state["nodes"] if n["id"] == "project")
        self.assertEqual(len(ui_node["jobs"]["rows"]), 65)
        self.assertEqual(ui_node["jobs"]["median_reviews"], 2)
        self.assertEqual(len([d for d in state["due"] if d["kind"] == "lesson-outcome"]), 65)
        # The live view adds only each job's branch name; everything else is `brain due --json`.
        self.assertEqual([d.get("branch") for d in state["due"] if d["kind"] == "lesson-outcome"][:2], ["job-0", "job-1"])
        self.assertEqual([{k: v for k, v in d.items() if k != "branch"} for d in state["due"]], json.loads(self.b(root, "due", "--json")))

    def test_ui_search_ranks_folds_and_pages(self):
        names = ["Mariana", "Bruno Ana", "Ánalyst", "Ana Lima", "ana", "Zed"]
        state = {"nodes": [{"id": f"n{i}", "title": t, "updated": f"2026-01-0{i + 1}"} for i, t in enumerate(names)]}
        found = self.m.ui_search(state, "node", "ana")
        # exact, then name starts (accent-insensitive, ties alphabetical), then a later word, then contains
        self.assertEqual([i["title"] for i in found["items"]], ["ana", "Ana Lima", "Ánalyst", "Bruno Ana", "Mariana"])
        self.assertEqual(found["total"], 5)
        self.assertEqual(self.m.ui_search(state, "node", "LIMA")["items"][0]["title"], "Ana Lima")
        self.assertEqual(self.m.ui_search(state, "node", "nothing like it")["items"], [])
        self.assertEqual([i["title"] for i in self.m.ui_search(state, "node", "")["items"]][:2], ["Zed", "ana"])  # recent first
        arabic = {"nodes": [{"id": "a", "title": "مدرسة أحمد"}]}
        self.assertEqual(len(self.m.ui_search(arabic, "node", "مدرسه احمد")["items"]), 1)  # ta marbuta and alef forms
        many = {"nodes": [{"id": f"p{i:02}", "title": f"Project {i:02}"} for i in range(45)],
                "specialists": [{"id": "rev", "node": "p00", "title": "Project reviewer", "jobs": []}]}
        pages = [self.m.ui_search(many, "node", "project", offset, 20) for offset in (0, 20, 40)]
        self.assertEqual([p["next"] for p in pages], [20, 40, None])
        self.assertEqual(sum(len(p["items"]) for p in pages), 45)
        self.assertEqual({i["kind"] for i in self.m.ui_search(many, "all", "project", 0, 100)["items"]}, {"node", "specialist"})
        self.assertEqual(self.m.ui_search(many, "specialist", "rev")["items"][0]["secondary"], "Project 00 · 0 hires")

    def test_ui_sender_comes_from_the_record_and_due_names_its_job(self):
        sender = self.m.inbox_sender
        self.assertEqual(sender({"kind": "inbox", "text": "2026-01-01 09:30 +01:00 from worker: question: which key?"}), "worker")
        self.assertEqual(sender({"kind": "blocked", "text": "Waiting from Morgan: a review"}), "")
        self.assertEqual(sender({"kind": "inbox", "text": "a question with no record of who from x: y"}), "")
        root = self.init("alice")
        self.b(root, "new", "project", "--title", "Project")
        inst = self.m.Instance(root)
        row = {"Job": "job-a", "Started": "2026-01-01T00:00:00+00:00", "Ended": "", "Hours": "", "Reviews": "0", "Outcome": ""}
        self.m.write_jobs(inst, inst.node("project"), [row])
        due = [{"node": "project", "kind": "handback", "text": "job-a: ready to merge", "job": self.m.job_key(row)}]
        with mock.patch.object(self.m, "due_items", side_effect=lambda inst, n, *a, **k: due if n["id"] == "project" else []):
            state = self.m.ui_state(self.m.Instance(root))
        self.assertEqual([d.get("branch") for d in state["due"] if d.get("job")], ["job-a"])
        (root / "nodes/project/inbox.md").write_text("- 2026-01-01 00:00 from worker: question: which key?\n")
        state = self.m.ui_state(self.m.Instance(root))
        self.assertEqual([i["from"] for i in state["inbox"] if i["kind"] == "inbox"], ["worker"])

    def test_ui_from_a_core_worktree_serves_that_worktree(self):
        other = self.tmp / "instance"
        (other / "core" / "bin").mkdir(parents=True)
        (other / "core" / "bin" / "brain").write_text("# the instance's own core\n")
        worktree = self.tmp / "core-worktree"
        worktree.mkdir()
        calls = []
        with mock.patch.object(self.m, "try_find_root", return_value=other), \
                mock.patch.object(self.m.os, "execv", side_effect=lambda *a: calls.append(a)), \
                mock.patch.dict(os.environ, {"BRAIN_NO_REEXEC": ""}):
            for git_entry, command, expected in (("file", "ui", 0), ("dir", "ui", 1), ("file", "status", 2)):
                os.environ["BRAIN_NO_REEXEC"] = ""   # a re-exec sets it for the process it starts
                shutil.rmtree(worktree / ".git", ignore_errors=True)
                (worktree / ".git").unlink() if (worktree / ".git").is_file() else None
                (worktree / ".git").write_text("gitdir: elsewhere\n") if git_entry == "file" else (worktree / ".git").mkdir()
                with mock.patch.object(self.m, "CORE", worktree), mock.patch.object(self.m.sys, "argv", ["brain", command]):
                    self.m.reexec_into_instance_core()
                self.assertEqual(len(calls), expected, (git_entry, command))

    def test_ui_policy_and_new_data_redact_home_terms(self):
        root = self.init("alice")
        self.b(root, "new", "project", "--title", "Project")
        (root / "nodes/project/never-name.txt").write_text("HiddenName\n")
        cfg = Path(self.env["BRAIN_CONFIG"]).parent
        (cfg / "subscriptions.json").write_text(json.dumps({"stop_percent":73, "subscriptions":[]}))
        (cfg / "usage.json").write_text(json.dumps({"poll_interval_seconds":300, "subscriptions":[], "accounts":{}}))
        with mock.patch.object(self.m, "due_items", return_value=[{"node":"project", "text":"HiddenName action", "reasons":["HiddenName"]}]):
            state = self.m.ui_state(self.m.Instance(root), scan=[{"name":"HiddenName", "path":"/tmp/HiddenName"}])
        self.assertNotIn("HiddenName", json.dumps({k:state[k] for k in ("due", "scan")}))
        self.assertEqual(state["usage_policy"], {"stop_percent":73, "max_age_seconds":600})
        (cfg / "subscriptions.json").write_text("not json")
        state = self.m.ui_state(self.m.Instance(root))
        self.assertIsNone(state["usage_policy"]["stop_percent"])



if __name__ == "__main__":
    unittest.main()
