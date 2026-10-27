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
    def test_shared_needs_sources_have_the_same_item_id(self):
        root = self.init("alice")
        self.b(root, "new", "app", "--title", "App")
        status = root / "nodes/app/status.md"
        with status.open("a") as f:
            f.write("\n## Blocked\n\n- Choose a color; waiting on operator; clears when chosen; "
                    "since: 2020-01-01 00:00 +00:00; recommendation: blue; default: gray\n")
        with (root / "nodes/app/inbox.md").open("a") as f:
            f.write("\n- 2026-01-01 00:00 +00:00 from worker: question: Which shape?\n")
        state = json.loads(self.b(root, "ui", "--json"))
        for item in (i for i in state["inbox"] if i["node"] == "app" and i["kind"] in ("blocked-overdue", "inbox")):
            due = next(d for d in state["due"] if d["node"] == "app" and d["kind"] == item["kind"])
            self.assertEqual(due.get("id"), item["id"])

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

    def test_complete_warm_fill_retains_real_metadata_and_usage_under_ten_seconds(self):
        import dashboard_acceptance as acceptance
        folder = self.tmp / 'acceptance'
        manifest = acceptance.prepare(folder, nodes=2, transcripts=8, events=50)
        before = acceptance.business_snapshot(folder / 'meadow')
        with mock.patch.dict(os.environ, acceptance.environment(folder), clear=True):
            inst = self.m.UIInstance(folder / 'meadow')
            spend = self.m.UISpend(inst)
            spend.discover()
            self.m.advance_warm(spend, seconds=10)
            self.assertTrue(spend.value['complete'])
            cache, inputs = self.m.UIStateCache(), self.m.UIInputs(inst)
            for _ in range(3):
                started = time.monotonic()
                spend.discover()
                self.m.advance_warm(spend, seconds=10)
                signature = inputs.signature()
                state = self.m.ui_state(self.m.UIInstance(inst.root),
                    cache.problems_for(inst, signature), machine_spend=spend.value,
                    cache=cache, node_keys=inputs.node_keys(inst, signature))
                acceptance.validate(state, manifest)
                self.assertLessEqual(time.monotonic() - started, 10)
            # Completion alone cannot qualify a metadata-only or truncated reading.
            with self.assertRaises(AssertionError):
                acceptance.validate({**state, 'spend': None}, manifest)
            with self.assertRaises(AssertionError):
                acceptance.validate({**state, 'items': {'all': []}}, manifest)
        self.assertEqual(before, acceptance.business_snapshot(inst.root))
    def test_json_cold_partial_warm_full_and_changed_visibility_is_unknown(self):
        root = self.init('demo')
        env = {**self.env, 'BRAIN_ROOT': str(root)}
        def fast():
            result = subprocess.run([sys.executable, '-B', str(CORE / 'bin/brain'), 'ui', '--json'],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        first = fast()
        self.assertTrue(first['brain']['filling_in'])
        self.assertEqual(first['problems'], 'unknown')
        self.assertEqual(first['readiness'], 'unknown')
        self.assertEqual(first['source']['kind'], 'initial-partial')
        self.assertIsNone(first['source']['age_seconds'])
        full = json.loads(self.b(root, 'ui', '--json', '--wait'))
        self.assertFalse(full['brain']['filling_in'])
        self.assertIsInstance(full['problems'], int)
        warm = fast()
        self.assertEqual(warm['source']['kind'], 'cached-full')
        self.assertEqual(warm['source']['built_at'], full['source']['built_at'])
        self.assertGreaterEqual(warm['source']['age_seconds'], 0)
        (root / 'never-name.txt').write_text('Sequoia\n')
        changed = fast()
        self.assertEqual(changed['problems'], 'unknown')
        self.assertEqual(changed['readiness'], 'unknown')
        self.assertNotIn('sequoia', json.dumps(changed).lower())
        self.b(root, 'ui', '--json', '--wait')  # join/finish the owned fill before fixture cleanup

    def test_json_failed_fill_stays_unknown_and_retains_failure_metadata(self):
        root = self.init('demo')
        module = __import__(self.m._owners['ui_state'].__package__ + '.ui_json', fromlist=['background'])
        with mock.patch.object(sys, 'argv', ['brain', 'ui']), \
                mock.patch.object(module, 'ui_json_build', side_effect=OSError('fixture failure')):
            module.ui_json_background(str(root), '[]')
        with mock.patch.object(module, 'ui_json_request_fill', return_value=True):
            state = module.ui_json_read(self.m.UIInstance(root), [], wait=False)
        self.assertEqual(state['problems'], 'unknown')
        self.assertEqual(state['readiness'], 'unknown')
        self.assertTrue(state['loading']['failed'])
        self.assertTrue(state['loading']['filling_in'])
        fresh = module.ui_json_read(self.m.UIInstance(root), [], wait=True)
        self.assertEqual(fresh['source']['kind'], 'fresh-full')
        warm = module.ui_json_read(self.m.UIInstance(root), [], wait=False)
        self.assertFalse(warm['loading']['failed'])
        self.assertEqual(warm['source']['kind'], 'cached-full')

    def test_json_wait_refused_optional_lock_still_builds_full_without_cache_or_instance_lock(self):
        root = self.init('demo')
        module = __import__(self.m._owners['ui_state'].__package__ + '.ui_json', fromlist=['read'])
        inst = self.m.UIInstance(root)
        foreign = self.tmp / 'refused-lock-target'
        foreign.write_text('retain fixture evidence')
        lock = root / '.brain/ui-cache/json-fill.lock'
        lock.parent.mkdir(exist_ok=True)
        lock.symlink_to(foreign)
        before = foreign.read_bytes(), lock.lstat().st_ino
        original = module.ui_json_build
        def checked_build(*args, **kwargs):
            self.assertIsNone(self.m._owners['brain_lock']._LOCK['fh'])
            self.assertFalse(kwargs['cache'])
            return original(*args, **kwargs)
        with mock.patch.object(module, 'ui_json_build', side_effect=checked_build), \
                mock.patch.object(module._m_ui_readmodel.ReadModel, 'write', side_effect=AssertionError('cache publication')):
            state = module.ui_json_read(inst, [], wait=True)
        self.assertEqual(state['source']['kind'], 'fresh-full')
        self.assertIsInstance(state['problems'], int)
        self.assertFalse(state['loading']['filling_in'])
        self.assertEqual(before, (foreign.read_bytes(), lock.lstat().st_ino))
        with mock.patch.object(module, 'ui_json_fill_lock', side_effect=PermissionError('optional lock')), \
                mock.patch.object(module, 'ui_json_build', side_effect=RuntimeError('source computation')):
            with self.assertRaisesRegex(RuntimeError, 'source computation'):
                module.ui_json_read(inst, [], wait=True)

    def test_json_background_fill_computes_without_instance_lock(self):
        root = self.init('demo')
        marker, release = self.tmp / 'filling', self.tmp / 'fill-release'
        script = '''import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from brain import ui_json, instance
root, marker, release = sys.argv[2:5]
original = ui_json.ui_json_build
def slow(*args, **kwargs):
    assert instance._LOCK['fh'] is None
    Path(marker).write_text('filling')
    deadline = time.monotonic() + 15
    while not Path(release).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('fill release missing')
        time.sleep(.01)
    return original(*args, **kwargs)
ui_json.ui_json_build = slow
ui_json.ui_json_background(root, '[]')
'''
        env = {**self.env, 'BRAIN_ROOT': str(root)}
        worker = subprocess.Popen([sys.executable, '-B', '-c', script, str(CORE / 'lib'),
                                   str(root), str(marker), str(release)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and worker.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists(), 'fill did not reach its computation')
            result = subprocess.run([sys.executable, '-B', str(CORE / 'bin/brain'), 'status', 'root', '--refresh'],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIsNone(worker.poll(), 'fill should still be waiting for the test release')
        finally:
            release.write_text('release')
            try:
                output, error = worker.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                worker.kill(); output, error = worker.communicate()
        self.assertEqual(worker.returncode, 0, output + error)
        self.assertTrue((root / '.brain/ui-cache/json-snapshot.json').is_file())

    def test_complete_build_is_a_detached_business_projection(self):
        root = self.init('demo')
        self.b(root, 'new', 'project', '--title', 'Field Guide')
        inst = self.m.UIInstance(root)
        node = inst.node('project')
        line = '- 2026-01-01 00:00 +00:00 from worker: question: Pick a shape? <!-- host-question:shape -->\n'
        queue = self.m.hook_question_queue(node)
        queue.mkdir(parents=True, exist_ok=True)
        (queue / 'shape.json').write_text(json.dumps({'line': line, 'marker': '<!-- host-question:shape -->'}))
        def business():
            return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in root.rglob('*') if p.is_file() and not p.is_symlink()
                    and '.git' not in p.parts and 'core' not in p.relative_to(root).parts}
        before = business()
        with mock.patch('fcntl.flock', side_effect=AssertionError('display acquired a business lock')), \
             mock.patch.object(self.m, 'usage_refresh', side_effect=AssertionError('provider call')):
            first = self.m.ui_state(inst, problems=0)
            again = self.m.ui_state(self.m.UIInstance(root), problems=0)
        self.assertEqual(before, business())
        self.assertTrue(any('Pick a shape?' in i['text'] for i in first['inbox']))
        self.assertEqual([i['id'] for i in first['due']], [i['id'] for i in again['due']])
        # Explicit command reads retain their delivery behavior after projection exits.
        self.m.inbox_lines(node)
        self.assertFalse((queue / 'shape.json').exists())
        self.assertIn('Pick a shape?', (node['dir'] / 'inbox.md').read_text())

    def test_selected_observation_copies_only_its_record_and_preserves_isolation(self):
        root = self.init('demo')
        module = self.m._owners['observe']
        selected = {'baseline': {'processed': 10}, 'nested': [1]}
        class OtherRecord:
            def __deepcopy__(self, memo):
                raise AssertionError('copied an unrelated lifecycle record')
        ledger = {'usage': {'selected': selected, 'other': OtherRecord()}}
        reader = mock.Mock(return_value=ledger)
        inst = self.m.UIInstance(root)
        path = root / '.brain/wake/lifecycle.json'
        with module.observe(inst) as view:
            first = view.store(path, reader, select=lambda data: data['usage']['selected'])
            first['baseline']['processed'] = 99
            first['nested'].append(2)
            second = view.store(path, reader, select=lambda data: data['usage']['selected'])
            self.assertEqual(second, selected)
            self.assertEqual(reader.call_count, 1)
        selected['baseline']['processed'] = 20
        with module.observe(inst) as view:
            self.assertEqual(view.store(path, reader, select=lambda data: data['usage']['selected']), selected)
        self.assertEqual(reader.call_count, 2)

    def test_readmodel_cache_validates_binding_and_keeps_metadata_separate_from_usage(self):
        ReadModel = self.m.ReadModel
        root = self.init('demo')
        model = ReadModel(self.m.UIInstance(root), 'code-one')
        self.assertIsNone(model.metadata([('source', 1)]))
        payload = {'brain': {'title': 'Field Guide'}, 'spend': {'complete': True}, 'nodes': []}
        with mock.patch.object(model, 'write') as write:
            model.save_metadata([('source', 1)], payload)
        record = write.call_args.args[1]
        self.assertNotIn('spend', record['state'])
        with mock.patch.object(model, 'read', return_value=record):
            self.assertEqual(model.metadata([('source', 1)])['brain']['title'], 'Field Guide')
            self.assertIsNone(model.metadata([('source', 2)]))
            other = ReadModel(self.m.UIInstance(root), 'code-two')
            with mock.patch.object(other, 'read', return_value=record):
                self.assertIsNone(other.metadata([('source', 1)]))

    def test_usage_cache_reopens_authorized_source_and_rejects_replaced_file(self):
        ReadModel = self.m.ReadModel
        root = self.init('demo')
        model = ReadModel(self.m.UIInstance(root), 'code-one')
        path = self.tmp / 'session.jsonl'
        path.write_text('{"timestamp":"2026-01-01T00:00:00Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":12,"output_tokens":3}}}}\n')
        # A cache hit still goes through spend_file's opened-descriptor ownership proof.
        spend = self.m._owners['spend_file']
        with mock.patch.object(model, 'write') as write:
            with model.usage_scope():
                first = spend.spend_file(self.m.UIInstance(root), path, 'codex', {self.tmp}, set())
        self.assertTrue(first['complete'])
        self.assertTrue(write.called)
        record = write.call_args.args[1]
        spend._SPEND_MEM.clear()
        with mock.patch.object(model, 'read', return_value=record), \
             mock.patch.object(spend, 'spend_read', side_effect=AssertionError('unchanged transcript replayed')):
            with model.usage_scope():
                second = spend.spend_file(self.m.UIInstance(root), path, 'codex', {self.tmp}, set())
        self.assertEqual(first['mc'], second['mc'])
        spend._SPEND_MEM.clear()
        with mock.patch.object(model, 'read', return_value=record):
            with model.usage_scope():
                self.assertIsNone(spend.spend_file(self.m.UIInstance(root), path, 'codex', set(), {self.tmp}))
        replacement = path.with_suffix('.new')
        replacement.write_text('{}\n')
        replacement.replace(path)
        spend._SPEND_MEM.clear()
        with mock.patch.object(model, 'read', return_value=record), mock.patch.object(model, 'write'):
            with model.usage_scope():
                third = spend.spend_file(self.m.UIInstance(root), path, 'codex', {self.tmp}, set())
        self.assertEqual(third['state']['offset'], 3)
        self.assertNotEqual(first['ino'], third['ino'])

    def test_ui_restart_projection_is_stable_and_preserves_native_receipt_identity(self):
        root = self.first_paint_root()
        inst = self.m.UIInstance(root)
        node = inst.nodes()[0]
        row = {'Job': 'job/palette', 'Started': '2026-01-01T00:00Z', 'Ended': ''}
        usage = {'processed': 200, 'compactions': 3}
        with mock.patch.object(self.m, 'live_usage', return_value=usage):
            with self.m.observe(inst):
                projected = self.m.usage_restart_item(inst, node, row)
                self.assertEqual(projected, self.m.usage_restart_item(inst, node, row))
                self.assertTrue(projected['protected'])
                self.assertFalse((root / '.brain/wake').exists())
            actual = self.m.usage_restart_item(inst, node, row)
            path = root / '.brain/wake/lifecycle.json'
            before = path.read_bytes()
            with self.m.observe(inst):
                self.assertEqual(self.m.usage_restart_item(inst, node, row), actual)
            self.assertEqual(path.read_bytes(), before)
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with self.m.observe(inst):
                raise RuntimeError('fixture')
        self.assertFalse(bool(self.m.projection(inst.root)))
        observations = []
        with self.m.observe(inst):
            worker = threading.Thread(target=lambda: observations.append(bool(self.m.projection(inst.root))))
            worker.start()
            worker.join(1)
            self.assertFalse(worker.is_alive())
        self.assertEqual(observations, [False])

    def test_ui_retains_pending_answer_until_command_applies_it_exactly_once(self):
        root = self.first_paint_root()
        inst = self.m.UIInstance(root)
        node = inst.nodes()[0]
        line = '- 2026-01-01 00:00 +00:00 from worker: question: Which color? <!-- host-question:color -->'
        (root / 'inbox.md').write_text(line + '\n')
        episode = self.m.request_episode_id(inst, node, line)
        folder = root / '.brain/wake/conversation-answers'
        folder.mkdir(parents=True)
        pending = folder / 'color.json'
        pending.write_text(json.dumps({'node': 'root', 'state': 'pending', 'marker': '<!-- host-question:color -->',
                                      'episode': episode, 'answer': 'Blue', 'source': 'conversation-option',
                                      'origin': 'fixture-origin', 'evidence': 'fixture-evidence', 'at': inst.now()}))
        before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        state = self.m.ui_state(inst, problems=0, machine_spend={})
        self.assertTrue(any('Which color?' in i['text'] for i in state['inbox']))
        self.assertEqual({str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}, before)
        self.m.inbox_lines(node)
        self.m.inbox_lines(node)
        self.assertFalse(pending.exists())
        self.assertTrue(pending.with_suffix('.done').exists())
        answers = self.m.lifecycle_read(inst)['answers']
        self.assertEqual([(a['id'], a['answer']) for a in answers], [(episode, 'Blue')])
        after = self.m.ui_state(self.m.UIInstance(root), problems=0, machine_spend={})
        self.assertFalse(any('Which color?' in i['text'] for i in after['inbox']))

    def test_ui_hold_projection_keeps_recorded_episode_and_command_claims_once(self):
        root = self.first_paint_root()
        inst = self.m.UIInstance(root)
        scope = self.m.machine_scope(inst, 'root')
        state = {'queued': 1, 'waiting_scopes': [scope], 'held': ['thermal pressure'], 'verify_limit': 1}
        path = root / '.brain/wake/machine-holds.json'
        with self.m.observe(inst):
            first = self.m.machine_hold_items(inst, state)
            self.assertEqual(first, self.m.machine_hold_items(inst, state))
        self.assertFalse(path.exists())
        self.assertFalse(path.with_suffix('.lock').exists())
        recorded = self.m.machine_hold_items(inst, state)
        before = path.read_bytes()
        with self.m.observe(inst):
            self.assertEqual(self.m.machine_hold_items(inst, state), recorded)
        self.assertEqual(path.read_bytes(), before)
        episode = recorded[scope[1]]['episode']
        self.assertTrue(self.m.machine_hold_claim(inst, episode, scope[1]))
        self.assertFalse(self.m.machine_hold_claim(inst, episode, scope[1]))

    def test_ui_frozen_observation_preserves_clock_and_reports_existing_stall(self):
        root = self.first_paint_root()
        self.git('init', '-q', '-b', 'main', cwd=root)
        inst = self.m.UIInstance(root)
        node = inst.nodes()[0]
        row = {'Job': 'job/palette', 'Started': '2026-01-01T00:00Z', 'Ended': ''}
        self.m.write_jobs(inst, node, [row])
        (root / 'status.md').write_text('## Active work\n\n| Branch | Thread | PR | State |\n'
                                       '| --- | --- | --- | --- |\n| job/palette | fixture | - | frozen |\n')
        now = datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc)
        with mock.patch.object(self.m.UIInstance, 'now_dt', return_value=now):
            with self.m.observe(inst):
                self.assertEqual(self.m.watch_job_stalls(inst, node), [])
            self.assertFalse(list((root / '.brain/wake').glob('job-stalls-*')))
            self.assertEqual(self.m.watch_job_stalls(inst, node), [])
        path = next((root / '.brain/wake').glob('job-stalls-*.json'))
        before = path.read_bytes()
        with mock.patch.object(self.m.UIInstance, 'now_dt', return_value=now + datetime.timedelta(minutes=16)):
            with self.m.observe(inst):
                items = self.m.watch_job_stalls(inst, node)
                self.assertEqual(items, self.m.watch_job_stalls(inst, node))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual([i['kind'] for i in items], ['job-stall'])

    def test_private_readmodel_cache_rejects_links_and_malformed_aggregates(self):
        root = self.init('demo')
        inst = self.m.UIInstance(root)
        model = self.m.ReadModel(inst, 'code-one')
        folder = root / '.brain/ui-cache'
        folder.mkdir(mode=0o700)
        outside = self.tmp / 'other.json'
        outside.write_text('{"private": "outside"}')
        (folder / 'metadata.json').symlink_to(outside)
        self.assertIsNone(model.read('metadata'))
        (folder / 'metadata.json').unlink()
        os.link(outside, folder / 'metadata.json')
        self.assertIsNone(model.read('metadata'))
        self.assertEqual(outside.read_text(), '{"private": "outside"}')
        spend = self.m._owners['spend_file']
        for corrupt in ({}, {'state': None}, {'state': {'offset': -1}}, [], None):
            self.assertFalse(spend.spend_cache_valid(corrupt, 'key', (1, 2), 'codex', [3, 4, 5, 6, 1], False))

    def test_usage_discovery_revokes_completion_before_next_slice_and_clears_revoked_totals(self):
        root = self.init('demo')
        inst = self.m.UIInstance(root)
        spend = self.m.UISpend(inst)
        spend.permissions = (frozenset({self.tmp}), frozenset())
        spend.value = {'complete': True, 'files': 1, 'read': 1, 'runtimes': {'codex': {'tokens': 20}}}
        path = self.tmp / 'sessions/session.jsonl'
        path.parent.mkdir()
        path.write_text('{}\n')
        with mock.patch.object(self.m, 'spend_sources', return_value=([('codex', self.tmp)], {self.tmp}, set())):
            spend.discover()
        self.assertFalse(spend.value['complete'])
        self.assertTrue(spend.queue)
        with mock.patch.object(self.m, 'spend_sources', return_value=([], set(), {self.tmp})):
            spend.discover()
        self.assertFalse(spend.value['complete'])
        self.assertEqual(spend.value['runtimes'], {})
        self.assertFalse(spend.aggs)

    def test_usage_completion_controls_full_dashboard_without_discarding_partial_values(self):
        import urllib.request
        root = self.init('demo')
        allowed = threading.Event()
        spend = mock.Mock(inst=self.m.UIInstance(root), value={'complete': False, 'files': 2, 'read': 1},
                          revision=0, permissions=None)
        spend.refresh_view.return_value = False
        def advance():
            if allowed.is_set() and not spend.value['complete']:
                spend.value = {'complete': True, 'files': 2, 'read': 2}
                return True
            return False
        spend.advance.side_effect = advance
        with self.integration_server(root, lambda inst: spend, cleanup=allowed.set) as (ready, get, workers, before):
            self.assertTrue(ready.wait(2))
            deadline = time.monotonic() + 5
            while True:
                with get('/api/state') as response:
                    state = json.load(response)
                if (state.get('spend') or {}).get('read') == 1:
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            self.assertTrue(state['brain']['filling_in'])
            self.assertEqual(state['spend']['files'], 2)
            with get('/api/loading') as response:
                self.assertTrue(json.load(response)['filling_in'])
            allowed.set()
            while state['brain'].get('filling_in'):
                self.assertLess(time.monotonic(), deadline)
                with get('/api/state') as response:
                    state = json.load(response)
            self.assertEqual(state['spend']['read'], 2)
            with get('/api/loading') as response:
                self.assertFalse(json.load(response)['filling_in'])

    @contextlib.contextmanager
    def integration_server(self, root, spend, cleanup=lambda: None, every=0.05):
        import http.server
        import urllib.request
        inst = self.m.UIInstance(root)
        ready = threading.Event()
        servers, workers, errors = [], [], []
        original_server = http.server.ThreadingHTTPServer
        original_record, original_start = self.m.ui_server_record, threading.Thread.start
        def server(*a, **kw):
            value = original_server(*a, **kw)
            servers.append(value)
            return value
        def record(*a, **kw):
            original_record(*a, **kw)
            ready.set()
        def start(thread):
            if getattr(thread._target, '__name__', '') == 'initialize':
                workers.append(thread)
            return original_start(thread)
        def run():
            try:
                self.m.cmd_ui(argparse.Namespace(json=False, scan=[], open=False, write=False, port=0, every=every))
            except BaseException as error:
                errors.append(error)
        def get(path):
            port = servers[0].server_address[1]
            data = json.loads((root / '.brain/ui-servers' / (str(port) + '.json')).read_text())
            return urllib.request.urlopen('http://127.0.0.1:' + str(port) + path + '?k=' + data['key'], timeout=2)
        with mock.patch.object(self.m, 'load', return_value=inst), \
             mock.patch.object(self.m, 'UISpend', side_effect=spend), \
             mock.patch.object(http.server, 'ThreadingHTTPServer', side_effect=server), \
             mock.patch.object(self.m, 'ui_server_record', side_effect=record), \
             mock.patch.object(threading.Thread, 'start', start), \
             contextlib.redirect_stdout(io.StringIO()):
            thread = threading.Thread(target=run, daemon=True)
            before = time.monotonic()
            thread.start()
            try:
                yield ready, get, workers, before
            finally:
                cleanup()
                # Release a blocked bootstrap even on the deliberately red path.
                ready.wait(0.5)
                if servers:
                    servers[0].shutdown()
                thread.join(2)
                for worker in workers:
                    worker.join(2)
                self.assertFalse(thread.is_alive(), 'owned server did not stop')
                self.assertFalse(any(worker.is_alive() for worker in workers), 'initialization worker leaked')
                self.assertEqual(errors, [])

    def test_partial_inbox_ignores_lifecycle_lock_and_preserves_queued_questions(self):
        import fcntl
        root = self.first_paint_root()
        inst = self.m.UIInstance(root)
        reminders = self.m._owners['lifecycle_store']
        pending = '- 2026-01-01 00:00 +00:00 from worker: question: Which shape?'
        answered = '- 2026-01-01 00:00 +00:00 from worker: question: Which color?'
        queued = '- 2026-01-01 00:00 +00:00 from worker: question: Which size? <!-- host-question:demo -->\n'
        for with_queue in (False, True):
            with self.subTest(queue=with_queue):
                inbox = root / 'inbox.md'
                inbox.write_text(pending + '\n' + answered + '\n')
                data = reminders.lifecycle_empty()
                key = reminders.inbox_source_id(inst.nodes()[0], answered)
                episode = 'a' * 24
                data['inbox']['root'] = {key: {'id': episode, 'answered': True}}
                data['answers'] = [{'id': episode, 'source_id': key, 'node': 'root', 'answer': 'Blue'}]
                folder = root / '.brain/wake'
                folder.mkdir(exist_ok=True)
                lifecycle = folder / 'lifecycle.json'
                lifecycle.write_text(json.dumps(data))
                queue = root / '.brain/host-questions/root'
                queue.mkdir(parents=True, exist_ok=True)
                if with_queue:
                    (queue / 'question.json').write_text(json.dumps({'line': queued, 'marker': '<!-- host-question:demo -->'}))
                def snapshot():
                    return (lifecycle.read_bytes(), inbox.read_bytes(),
                            {p.name: p.read_bytes() for p in queue.iterdir()})
                before = snapshot()
                full_entered, release_full = threading.Event(), threading.Event()
                spend = mock.Mock(inst=inst, value={}, revision=0, permissions=None)
                spend.advance.return_value = False
                spend.refresh_view.return_value = False
                def discover():
                    full_entered.set()
                    if not release_full.wait(2):
                        raise RuntimeError('full initialization barrier not released')
                spend.discover.side_effect = discover
                lock = (folder / 'lifecycle.lock').open('a+')
                fcntl.flock(lock, fcntl.LOCK_EX)
                def release():
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    release_full.set()
                stream = None
                try:
                    with self.integration_server(root, lambda _: spend, release) as (ready, get, workers, start):
                        try:
                            bound = ready.wait(0.5)
                            untouched = snapshot() == before
                            # Keep independent side-effect evidence on the old blocked path.
                            print('held lifecycle queue=', with_queue, 'bound=', bound, 'untouched=', untouched, file=sys.stderr)
                            self.assertTrue(untouched, 'partial bootstrap published lifecycle or drained queued questions')
                            self.assertTrue(bound, 'partial bootstrap waited for lifecycle lock before bind')
                            self.assertTrue(full_entered.wait(0.5))
                            with get('/api/state') as response:
                                partial = json.load(response)
                            self.assertTrue(partial['brain']['filling_in'])
                            self.assertEqual([n['title'] for n in partial['nodes']], ['Alice workspace', 'Meadow'])
                            lines = partial['nodes'][0]['inbox']
                            self.assertIn(pending[2:], lines)
                            self.assertNotIn(answered[2:], lines)
                            self.assertEqual(queued.rstrip()[2:] in lines, with_queue)
                            stream = get('/api/events')
                            self.assertEqual(json.loads(stream.readline()[6:]), partial)
                            self.assertLess(time.monotonic() - start, 1)
                            self.assertEqual(snapshot(), before)
                            release()
                            full = partial
                            while full['brain'].get('filling_in'):
                                line = stream.readline()
                                if line.startswith(b'data: '):
                                    full = json.loads(line[6:])
                            self.assertNotIn(answered[2:], full['nodes'][0]['inbox'])
                            self.assertEqual(snapshot(), before)
                            # Only the explicit ordinary reader applies queued business state.
                            self.m.inbox_lines(inst.nodes()[0])
                            self.assertNotEqual(lifecycle.read_bytes(), before[0])
                            if with_queue:
                                self.assertEqual(list(queue.iterdir()), [])
                                self.assertEqual(inbox.read_text().count('<!-- host-question:demo -->'), 1)
                        finally:
                            if stream:
                                stream.close()
                finally:
                    release()
                    lock.close()

    def test_initial_failure_recovers_on_same_server_refresh_without_another_worker(self):
        real_state = self.m.ui_state
        for stage in ('discover', 'state'):
            with self.subTest(stage=stage):
                root = self.first_paint_root('recovery-' + stage)
                fixed = self.m.UIInstance(root).now_dt()
                failed, retry_entered, release_retry = threading.Event(), threading.Event(), threading.Event()
                attempts, active = [], [0, 0]
                sentinel = 'fabricated-private-initialization-error'
                spend = mock.Mock(inst=self.m.UIInstance(root), value={}, revision=0, permissions=None)
                spend.advance.return_value = False
                spend.refresh_view.return_value = False
                calls = [0]
                def fault():
                    calls[0] += 1
                    attempts.append(threading.get_ident())
                    active[0] += 1
                    active[1] = max(active[1], active[0])
                    try:
                        if calls[0] == 1:
                            failed.set()
                            raise OSError(sentinel)
                        retry_entered.set()
                        self.assertTrue(release_retry.wait(2))
                    finally:
                        active[0] -= 1
                def state(*a, **kw):
                    if stage == 'state':
                        fault()
                    return real_state(*a, **kw)
                if stage == 'discover':
                    spend.discover.side_effect = fault
                stream = None
                # Compare publication contents at one instance time; request/retry
                # scheduling still uses the real monotonic clock.
                with mock.patch.object(self.m.UIInstance, 'now_dt', return_value=fixed), \
                     mock.patch.object(self.m, 'ui_state', side_effect=state):
                    with self.integration_server(root, lambda _: spend, release_retry.set) as (ready, get, workers, start):
                        try:
                            self.assertTrue(ready.wait(0.5))
                            stream = get('/api/events')
                            partial = json.loads(stream.readline()[6:])
                            self.assertTrue(partial['brain']['filling_in'])
                            self.assertTrue(failed.wait(0.5))
                            until = time.monotonic() + 0.5
                            while True:
                                with get('/api/loading') as response:
                                    loading = json.load(response)
                                if loading['failed'] or time.monotonic() >= until:
                                    break
                            self.assertTrue(loading['failed'])
                            self.assertNotIn(sentinel, json.dumps((loading, partial)))
                            with get('/') as response:
                                self.assertNotIn(sentinel.encode(), response.read())
                            # This real Refresh request must retain partial while the retry is held.
                            with get('/api/state') as response:
                                self.assertEqual(json.load(response), partial)
                            self.assertLess(time.monotonic() - start, 1)
                            self.assertTrue(retry_entered.wait(0.5), 'same-server Refresh did not retry initialization')
                            release_retry.set()
                            complete = partial
                            while complete['brain'].get('filling_in'):
                                line = stream.readline()
                                if line.startswith(b'data: '):
                                    complete = json.loads(line[6:])
                            with get('/api/state') as response:
                                self.assertEqual(json.load(response), complete)
                                self.assertGreater(int(response.headers['X-Brain-UI-Version']), 1)
                            with get('/api/loading') as response:
                                status = json.load(response)
                            self.assertFalse(status['filling_in'])
                            self.assertFalse(status['failed'])
                            self.assertFalse(status['refreshing'])
                            self.assertNotIn(sentinel, json.dumps((complete, status)))
                            self.assertEqual(len(workers), 1)
                            self.assertEqual(len(set(attempts)), 1)
                            self.assertEqual(calls[0], 2)
                            self.assertEqual(active[1], 1)
                        finally:
                            if stream:
                                stream.close()

    def test_failed_initialization_waits_for_demand_coalesces_and_stops(self):
        root = self.first_paint_root()
        retry_entered, release_retry = threading.Event(), threading.Event()
        waiting = threading.Event()
        attempts, workers_seen = [], []
        wait = threading.Condition.wait
        def observed_wait(condition, timeout=None):
            if threading.current_thread() in workers_seen and timeout is None:
                waiting.set()
            return wait(condition, timeout)
        spend = mock.Mock(inst=self.m.UIInstance(root), value={}, revision=0, permissions=None)
        def discover():
            attempts.append((threading.get_ident(), time.monotonic()))
            if len(attempts) == 2:
                retry_entered.set()
                self.assertTrue(release_retry.wait(2))
            raise OSError('fabricated transient failure')
        spend.discover.side_effect = discover
        with mock.patch.object(threading.Condition, 'wait', observed_wait):
            with self.integration_server(root, lambda _: spend, release_retry.set) as (ready, get, workers, start):
                workers_seen = workers
                self.assertTrue(ready.wait(0.5))
                self.assertTrue(waiting.wait(0.5), 'failed initializer did not remain available for refresh')
                self.assertEqual(len(attempts), 1)
                for _ in range(3):
                    with get('/api/loading') as response:
                        self.assertTrue(json.load(response)['failed'])
                self.assertEqual(len(attempts), 1, 'status reads retried initialization')
                with get('/api/state') as response:
                    self.assertTrue(json.load(response)['brain']['filling_in'])
                self.assertTrue(retry_entered.wait(0.5))
                for _ in range(4):
                    with get('/api/state') as response:
                        self.assertTrue(json.load(response)['brain']['filling_in'])
                self.assertEqual(len(attempts), 2, 'refresh burst created duplicate workers')
                waiting.clear()
                release_retry.set()
                self.assertTrue(waiting.wait(0.5), 'failed retry did not sleep after coalesced demand')
                self.assertEqual(len(attempts), 3)
                self.assertGreaterEqual(attempts[1][1] - attempts[0][1], 0.05)
                self.assertGreaterEqual(attempts[2][1] - attempts[1][1], 0.05)
                self.assertEqual(len(set(tid for tid, _ in attempts)), 1)
                self.assertEqual(len(workers), 1)
            self.assertEqual(len(attempts), 3, 'shutdown restarted initialization')

    def first_paint_root(self, name='alice'):
        root = self.tmp / name
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

    def test_closed_worktrees_keep_metadata_without_git_or_socket_checks(self):
        module = self.m._owners['cached_worktree']
        root = self.first_paint_root()
        inst = self.m.UIInstance(root)
        node = inst.nodes()[0]
        record = {'path': str(root), 'port': 4747}
        node['local']['worktrees'] = {'job/closed': record, 'job/live': record}
        rows = [{'Job': 'job/closed', 'Ended': '2026-01-01'}, {'Job': 'job/live', 'Ended': ''}]
        cache = module.UIStateCache()
        with mock.patch.object(module._m_jobs, 'read_jobs', return_value=rows), \
             mock.patch.object(module._m_status, 'active_work', return_value=[]), \
             mock.patch.object(module, 'ui_worktree', return_value={'branch': 'job/live'}) as inspect:
            closed = module.cached_worktree(inst, root, 'job/closed', record, node, cache)
            inspect.assert_not_called()
            self.assertEqual(closed['branch'], 'job/closed')
            self.assertTrue(closed['exists'])
            self.assertIsNone(closed['dirty'])
            self.assertFalse(closed['live'])
            module.cached_worktree(inst, root, 'job/live', record, node, cache)
            inspect.assert_called_once()
            with mock.patch.object(module._m_status, 'active_work', return_value=[['job/closed', 'old thread', '-', 'Review']]):
                self.assertEqual(set(module.ui_live_worktrees(node)), {'job/live'})

    def test_due_repository_snapshot_batches_refs_and_display_cache_never_leaks_to_actions(self):
        module = self.m._owners['local_job_state']
        root = self.first_paint_root()
        home = self.repo(self.tmp / 'repository')
        inst = self.m.UIInstance(root)
        node = inst.nodes()[0]
        node['home_path'] = home
        tip = self.git('rev-parse', 'HEAD', cwd=home).strip()
        self.git('branch', 'job/merged', cwd=home)
        record = {'path': str(home), 'start': '0' * 40}
        values, keys = {}, {('root', 'job/merged'): 'first'}
        real_git = module._m_common.git
        with mock.patch.object(module._m_common, 'git', wraps=real_git) as git:
            with module.local_job_snapshot(values, keys):
                first = module.local_job_state(node, 'job/merged', record)
                for i in range(160):
                    module.local_job_state(node, 'job/missing-' + str(i), record)
            self.assertEqual(first, {'exists': True, 'tip': tip, 'merged': True})
            self.assertLess(git.call_count, 8, 'one ref scan per node, not commands per worktree')
            git.reset_mock()
            with module.local_job_snapshot(values, keys):
                self.assertEqual(module.local_job_state(node, 'job/merged', record), first)
            git.assert_not_called()
            self.git('branch', '-D', 'job/merged', cwd=home)
            self.assertEqual(module.local_job_state(node, 'job/merged', record)['tip'], '')
            self.assertGreater(git.call_count, 0, 'an explicit action must read current refs')
            keys[('root', 'job/merged')] = 'second'
            with module.local_job_snapshot(values, keys):
                self.assertEqual(module.local_job_state(node, 'job/merged', record)['tip'], '')

    def test_input_tree_reuses_inventory_and_detects_nested_changes(self):
        module = self.m._owners['UIInputs']
        root = self.tmp / 'inputs'
        nested = root / 'nested'
        nested.mkdir(parents=True)
        source = nested / 'source.md'
        source.write_text('one')
        tree = module.UIInputTree()
        with mock.patch.object(module.os, 'scandir', wraps=os.scandir) as scan:
            first = tree.paths(root)
            scans = scan.call_count
            self.assertEqual(tree.paths(root), first)
            self.assertEqual(scan.call_count, scans)
            old = module.UIInputs.stamp(source)
            source.write_text('changed in place')
            self.assertNotEqual(module.UIInputs.stamp(source), old)
            added = nested / 'added.md'
            added.write_text('new')
            self.assertIn(added, tree.paths(root))
            self.assertGreater(scan.call_count, scans)
            source.unlink()
            self.assertNotIn(source, tree.paths(root))
            shutil.rmtree(nested)
            self.assertNotIn(added, tree.paths(root))
        outside = self.tmp / 'outside'
        outside.mkdir()
        (outside / 'private.md').write_text('outside')
        nested.symlink_to(outside, target_is_directory=True)
        self.assertIn(nested, tree.paths(root))
        self.assertNotIn(nested / 'private.md', tree.paths(root))

    def test_input_discovery_scans_only_live_worktrees_and_reopens_on_ledger_change(self):
        module = self.m._owners['UIInputs']
        root = self.first_paint_root()
        inst = self.m.UIInstance(root)
        node = inst.nodes()[0]
        node['home_path'], node['external'] = root, True
        records = {f'job/closed-{i}': {'path': str(root / f'closed-{i}')} for i in range(160)}
        records['job/live'] = {'path': str(root / 'live')}
        node['local']['worktrees'] = records
        rows = [{'Job': 'job/live', 'Ended': ''}]
        with mock.patch.object(module._m_jobs, 'read_jobs', side_effect=lambda _: list(rows)), \
             mock.patch.object(module._m_status, 'active_work', return_value=[]), \
             mock.patch.object(module._m_common, 'git', return_value=subprocess.CompletedProcess([], 0, '', '')) as git:
            inputs = module.UIInputs(inst)
            inspected = {call.kwargs.get('cwd') for call in git.call_args_list}
            self.assertEqual(inspected, {root, root / 'live'})
            before = inputs.topology
            rows.append({'Job': 'job/closed-1', 'Ended': ''})
            self.assertNotEqual(module.UIInputs.topology_key(inst), before)
            rows[:] = []
            with mock.patch.object(module._m_status, 'active_work', return_value=[['`job/closed-2`', 'thread', '-', 'working']]):
                self.assertEqual(set(module.ui_live_worktrees(node)), {'job/closed-2'})

    def test_indexed_inputs_preserve_full_walk_inventory_and_file_currentness(self):
        module = self.m._owners['UIInputs']
        root = self.first_paint_root()
        inst = self.m.UIInstance(root)
        inputs = module.UIInputs(inst)
        sources = root / 'sessions'
        nested = sources / 'nested'
        nested.mkdir(parents=True)
        source = nested / 'session.jsonl'
        source.write_text('{}\n')
        inputs.source_trees = [sources]

        def full_walk(top, ignored_dirs=None, ignored_files=None):
            paths = {top}
            for directory, dirs, files in os.walk(top):
                directory = Path(directory)
                dirs[:] = [name for name in dirs if name not in (ignored_dirs or {}).get(directory, ())]
                paths.update(directory / name for name in dirs)
                paths.update(directory / name for name in files
                             if name not in (ignored_files or {}).get(directory, ()))
            return paths

        def compare():
            actual = inputs.signature(), inputs.source_signature()
            with mock.patch.object(inputs.inventory, 'paths', side_effect=full_walk):
                self.assertEqual(actual, (inputs.signature(), inputs.source_signature()))
            return actual

        before = compare()
        parent_stamp = module.UIInputs.stamp(nested)
        source.write_text('{"changed":true}\n')
        self.assertEqual(module.UIInputs.stamp(nested), parent_stamp)
        changed = compare()
        self.assertNotEqual(before, changed, 'in-place writes must invalidate cached inventories')
        (nested / 'new.jsonl').write_text('{}\n')
        added = compare()
        self.assertNotEqual(changed, added)
        shutil.rmtree(nested)
        self.assertNotEqual(added, compare())

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

    def test_open_reuses_then_replaces_an_authenticated_listener_on_the_requested_port(self):
        import http.server
        import socket
        import webbrowser
        root = self.first_paint_root()
        inst = self.m.UIInstance(root)
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        servers, threads, workers, opened, errors = [], [], [], [], []
        opened_event = threading.Event()
        original_server, original_start = http.server.ThreadingHTTPServer, threading.Thread.start
        def server(*args, **kwargs):
            value = original_server(*args, **kwargs)
            servers.append(value)
            return value
        def start(thread):
            if getattr(thread._target, '__name__', '') == 'initialize':
                workers.append(thread)
            return original_start(thread)
        def open_url(url):
            opened.append(url)
            opened_event.set()
        def run():
            try:
                self.m.cmd_ui(argparse.Namespace(json=False, scan=[], open=True, write=False, port=port, every=0.05))
            except BaseException as error:
                errors.append(error)
        with mock.patch.object(self.m, 'load', return_value=inst), \
             mock.patch.object(self.m, 'UISpend', side_effect=RuntimeError('synthetic initialization held')), \
             mock.patch.object(self.m, 'ui_code_version', side_effect=['old', 'old', 'new']), \
             mock.patch.object(http.server, 'ThreadingHTTPServer', side_effect=server), \
             mock.patch.object(threading.Thread, 'start', start), \
             mock.patch.object(webbrowser, 'open', side_effect=open_url), \
             contextlib.redirect_stdout(io.StringIO()):
            try:
                threads.append(threading.Thread(target=run, daemon=True))
                threads[-1].start()
                self.assertTrue(opened_event.wait(2), 'first owned listener did not start')
                run()
                self.assertEqual(errors, [])
                self.assertEqual(opened, [opened[0], opened[0]])
                self.assertEqual(len(servers), 1, 'matching version opened another listener')
                opened_event.clear()
                threads.append(threading.Thread(target=run, daemon=True))
                threads[-1].start()
                self.assertTrue(opened_event.wait(3), 'stale owned listener was not replaced')
                threads[0].join(1)
                self.assertFalse(threads[0].is_alive())
                self.assertEqual(errors, [])
                record = json.loads(self.m.ui_server_file(inst, port).read_text())
                self.assertEqual(record['version'], 'new')
                self.assertNotEqual(opened[-1], opened[0], 'replacement retained obsolete authentication key')
                self.assertTrue(all(server.server_address[1] == port for server in servers))
                self.assertEqual(self.m.ui_server_request(port, record['key'])['version'], 'new')
            finally:
                for owned in servers:
                    if owned.fileno() >= 0:
                        owned.shutdown()
                for thread in threads + workers:
                    thread.join(2)
                self.assertFalse(any(thread.is_alive() for thread in threads + workers))
        self.assertEqual(errors, [])
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(('127.0.0.1', port)), 0, 'owned listener leaked')

    def test_preview_tour_receipt_binds_source_and_artifacts_and_never_clears_on_a_bare_url(self):
        import hashlib
        root = self.first_paint_root()
        home = self.repo(self.tmp / 'preview-source')
        inst = self.m.UIInstance(root)
        node = inst.nodes()[0]
        node['home_path'], node['external'] = home, True
        node['local']['preview'] = 'http://127.0.0.1:4999/'
        folder = self.tmp / 'preview-evidence'
        folder.mkdir()
        image = folder / 'overview.png'
        # A synthetic PNG header; the receipt checks format and bytes, not visual quality.
        image.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\0' * 4 + b'IHDR' + (1280).to_bytes(4, 'big') + (720).to_bytes(4, 'big'))
        tour = folder / 'index.md'
        tour.write_text('# Preview tour\n\n![Overview](overview.png)\n')
        node['local']['preview_tour'] = str(tour)
        module = self.m._owners['cmd_report']
        self.assertEqual(module.preview_evidence(inst, node)['state'], 'missing')
        def preview_due():
            state = {'jobs': [], 'worktrees': {}, 'handbacks': {}, 'inbox': []}
            with mock.patch.object(self.m, 'due_evidence', return_value=(state, '', '')):
                return [row['kind'] for row in self.m.due_items(inst, node, include_stale=False, include_advice=False)]
        self.assertIn('preview-evidence', preview_due())
        def artifact(path):
            return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        receipt = {'version': 1, 'url': node['local']['preview'], 'branch': '',
                   'head': self.git('rev-parse', 'HEAD', cwd=home).strip(),
                   'checked_at': inst.now_dt().isoformat(), 'tour': artifact(tour), 'screenshots': [artifact(image)]}
        input_path = folder / 'receipt.json'
        input_path.write_text(json.dumps(receipt))
        with mock.patch.object(module._m_nodes, 'load', return_value=inst), contextlib.redirect_stdout(io.StringIO()):
            module.cmd_preview(argparse.Namespace(id='root', record=str(input_path), json=False))
        self.assertEqual(module.preview_evidence(inst, node)['state'], 'verified')
        self.assertNotIn('preview-evidence', preview_due())
        ui = self.m._owners['UIInputs']
        inputs = ui.UIInputs(inst)
        before_key = inputs.node_keys(inst, inputs.signature())['root']
        image.write_bytes(image.read_bytes() + b'changed')
        self.assertNotEqual(inputs.node_keys(inst, inputs.signature())['root'], before_key,
                            'external screenshot metadata did not invalidate cached preview evidence')
        self.assertEqual(module.preview_evidence(inst, node)['state'], 'stale')
        self.assertIn('preview-evidence', preview_due())
        image.write_bytes(image.read_bytes()[:-7])
        self.git('commit', '--allow-empty', '-qm', 'next preview revision', cwd=home)
        self.assertEqual(module.preview_evidence(inst, node)['state'], 'stale')
        before = module.preview_receipt_file(inst, node).read_bytes()
        receipt['head'] = self.git('rev-parse', 'HEAD', cwd=home).strip()
        receipt['screenshots'] = []
        input_path.write_text(json.dumps(receipt))
        with mock.patch.object(module._m_nodes, 'load', return_value=inst), self.assertRaises(SystemExit):
            module.cmd_preview(argparse.Namespace(id='root', record=str(input_path), json=False))
        self.assertEqual(module.preview_receipt_file(inst, node).read_bytes(), before)
        receipt['screenshots'] = [artifact(image)]
        for changed in ({'url': 'https://example.invalid/other'}, {'branch': 'unbound'},
                        {'head': '0' * 40}, {'checked_at': 'not-a-date'}, {'version': True},
                        {'screenshots': [artifact(image), artifact(image)]}):
            with self.subTest(changed=list(changed)), self.assertRaises(ValueError):
                module.validate_preview_evidence(inst, node, {**receipt, **changed})
        node['local']['preview_tour'] = str(folder / 'another.md')
        with self.assertRaises(ValueError):
            module.validate_preview_evidence(inst, node, receipt)

    def test_round_usage_payload_keeps_lifetime_and_reports_unknown_receipts(self):
        module = self.m._owners['ui_node']
        root = self.first_paint_root()
        inst = self.m.UIInstance(root)
        node = inst.nodes()[0]
        row = {'Job': 'job/live', 'Started': '2026-01-01T00:00:00Z', 'Ended': ''}
        total = {'sessions': 1, 'processed': 900000, 'compactions': 8}
        round_usage = {'processed': 2000, 'compactions': 0, 'hours': 0.25}
        with mock.patch.object(module._m_jobs, 'jobs_summary', side_effect=lambda *_: {'open': [dict(row)], 'forecasts': []}), \
             mock.patch.object(module._m_spend, 'live_usage', return_value=total), \
             mock.patch.object(module._m_spend, 'spend_summary', side_effect=lambda value, _: value), \
             mock.patch.object(module._m_spend, 'job_round_usage', create=True, return_value=round_usage) as rounds:
            output = module.ui_node(inst, node, '')[0]['jobs']['open'][0]
            self.assertEqual(output['usage'], total)
            self.assertEqual(output['round_usage'], round_usage)
            rounds.assert_called_once()
            rounds.side_effect = ValueError('private invalid receipt detail')
            output = module.ui_node(inst, node, '')[0]['jobs']['open'][0]
            self.assertEqual(output['usage'], total)
            self.assertEqual(output['round_usage'], {'state': 'unavailable', 'processed': None, 'compactions': None, 'hours': None})
            self.assertNotIn('private invalid receipt detail', json.dumps(output))

    def test_round_receipt_metadata_invalidates_only_its_job_node_cache(self):
        module = self.m._owners['UIInputs']
        root = self.first_paint_root()
        other = root / 'nodes/amber'
        other.mkdir()
        (other / 'node.md').write_text('---\nid: amber\ntitle: Amber\n---\n')
        inst = self.m.UIInstance(root)
        row = {'Job': 'job/live', 'Started': '2026-01-01T00:00:00Z', 'Ended': ''}
        with mock.patch.object(module._m_jobs, 'read_jobs', side_effect=lambda n: [row] if n['id'] == 'meadow' else []):
            inputs = module.UIInputs(inst)
            before = inputs.node_keys(inst, inputs.signature())
            path = root / '.brain/wake/lifecycle.json'
            path.parent.mkdir(exist_ok=True)
            key = module._m_jobs.digest(['meadow', module._m_jobs.job_key(row)])
            path.write_text(json.dumps({'usage': {key: {'job': module._m_jobs.job_key(row), 'round_at': '2026-01-02T00:00:00Z'}}}))
            after = inputs.node_keys(inst, inputs.signature())
            self.assertNotEqual(before['meadow'], after['meadow'])
            self.assertEqual(before['amber'], after['amber'])

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
        def files():
            # Reminder lifecycle receipts reconcile on reads. Completion facts
            # and their delivery state must remain untouched by the dashboard.
            paths = [node['dir'] / 'job-ledger.md']
            for pattern in ('events*', 'watch*'):
                for path in (root / '.brain').glob(pattern):
                    paths.extend(path.rglob('*') if path.is_dir() else [path])
            return {str(p.relative_to(root)): p.read_bytes() for p in paths
                    if p.is_file() and not p.is_symlink()}
        before = files()
        with mock.patch.object(self.m, "usage_refresh", side_effect=AssertionError("UI must never poll")), \
             mock.patch.object(self.m, "wake_send", side_effect=AssertionError("UI must never call a host")):
            state = self.m.ui_state(inst)
        self.assertEqual(files(), before)
        ui_node = next(n for n in state["nodes"] if n["id"] == "project")
        self.assertEqual(len(ui_node["jobs"]["rows"]), 65)
        self.assertEqual(ui_node["jobs"]["median_reviews"], 2)
        self.assertEqual(len([d for d in state["due"] if d["kind"] == "lesson-outcome"]), 65)
        landed = [d['id'] for d in state['due'] if d['kind'] == 'job-landed']
        self.assertEqual(len(landed), 65)
        self.assertEqual(len(set(landed)), 65)
        # The live view adds branch names; stable item IDs also appear in `brain due --json`.
        self.assertEqual([d.get("branch") for d in state["due"] if d["kind"] == "lesson-outcome"][:2], ["job-0", "job-1"])
        self.assertEqual([{k: v for k, v in d.items() if k != "branch"} for d in state["due"]], json.loads(self.b(root, "due", "--json")))
        after_cli = files()
        with mock.patch.object(self.m, "usage_refresh", side_effect=AssertionError("UI must never poll")), \
             mock.patch.object(self.m, "wake_send", side_effect=AssertionError("UI must never call a host")):
            again = self.m.ui_state(self.m.Instance(root))
        self.assertEqual([d['id'] for d in again['due'] if d['kind'] == 'job-landed'], landed)
        self.assertEqual(files(), after_cli)

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
