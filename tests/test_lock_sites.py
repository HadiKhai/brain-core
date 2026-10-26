"""Per-site contention proofs with synthetic instances and explicit release barriers.

The child pauses at the actual slow call boundary. The parent runs real CLI
writers/readers on a sibling node, and checks both timing and eventual mutation.
The hold-lock variant is a mutation control: the same probe must time out.
"""
import contextlib
import json
import os
import signal
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import core_loader
from scenario import Scenario

CORE = Path(os.environ.get("BRAIN_CORE") or Path(__file__).resolve().parent.parent)
SITES = (
    "sync_fetch", "sync_prs", "rm_prs", "prune_fetch", "prune_prs",
    "rm_accounting", "prune_accounting", "job_accounting", "study_accounting", "handback_accounting",
    "spend_node", "spend_machine", "history_out", "connect_default", "connect_plan", "connect_existing_plan", "connect_save",
    "brief_usage", "hire_usage", "review_usage", "machine_install", "machine_uninstall",
)


def lock_worker(spec_path):
    spec = json.loads(Path(spec_path).read_text())
    site = spec["site"]
    core = Path(os.environ.get("BRAIN_LOCK_PROOF_CORE") or CORE)
    m = core_loader.SourceFileLoader("lock_site_worker", str(core / "bin/brain")).load_module()
    real_run, real_git = subprocess.run, m.git
    fired = False

    def slow():
        nonlocal fired
        if fired:
            return
        fired = True
        Path(spec["marker"]).write_text("ready")
        if spec.get("release"):
            script = ("import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); end=time.monotonic()+30; "
                      "exec('while not p.exists() and time.monotonic()<end: time.sleep(0.02)')")
            real_run([sys.executable, "-c", script, spec["release"]], check=True)
        else:
            real_run([sys.executable, "-c", "import time; time.sleep(30)"], check=True)

    def delayed(original):
        def call(*a, **kw):
            slow()
            return original(*a, **kw)
        return call

    def git(*a, **kw):
        if a and a[0] == "fetch":
            if site in ("sync_fetch", "prune_fetch"):
                slow()
            return subprocess.CompletedProcess(a, 0, "", "")
        return real_git(*a, **kw)

    def run(a, *args, **kw):
        if a and a[0] == "gh":
            if site in ("sync_prs", "rm_prs", "prune_prs"):
                slow()
                if spec.get("timeout"):
                    raise subprocess.TimeoutExpired(a, 30)
            return subprocess.CompletedProcess(a, 0, "[]", "")
        if a and a[0] == "crontab":
            slow()
            if a[-1] == "-":
                Path(spec["marker"] + ".mutation").write_text(site)
            return subprocess.CompletedProcess(a, 0, "", "")
        return real_run(a, *args, **kw)

    @contextlib.contextmanager
    def keep_locked(inst, *a, **kw):
        with m.brain_lock(inst):
            yield

    with contextlib.ExitStack() as stack:
        # Exercise lock/accounting boundaries independently of lifecycle admission.
        # Runtime removal retains its real refusal policy, covered by worktree tests.
        if site.startswith(("rm_", "prune_")):
            stack.enter_context(mock.patch.object(m, "removal_refusal", return_value=""))
        stack.enter_context(mock.patch.object(m, "git", side_effect=git))
        stack.enter_context(mock.patch.object(m.subprocess, "run", side_effect=run))
        which = m.shutil.which
        stack.enter_context(mock.patch.object(m.shutil, "which", side_effect=lambda x: "gh" if x == "gh" else which(x)))
        helper = None
        if "accounting" in site:
            helper = "live_usage" if site == "handback_accounting" else "job_usage"
        elif site.startswith("spend_"):
            helper = "spend_all"
        elif site in ("history_out", "history_reader"):
            helper = "history_brief"
        elif site in ("connect_default", "connect_plan"):
            helper = "connect_sources"
        elif site == "connect_existing_plan":
            helper = "connect_load"
        elif site in ("connect_save", "connect_reader"):
            helper = "connect_brief"
        elif site.endswith("_usage") or site == "usage_reader":
            helper = "usage_refresh"
            stack.enter_context(mock.patch.object(m, "usage_reading", return_value=("fixture-account", {
                "read_at": time.time(), "source": "lookup", "note": "", "windows": {
                    "five_hour": {"used_percent": 10, "resets_at": time.time() + 3600,
                                  "read_at": time.time(), "source": "lookup"}}})))
        if helper:
            stack.enter_context(mock.patch.object(m, helper, side_effect=delayed(getattr(m, helper))))
        if site.startswith("machine_"):
            stack.enter_context(mock.patch.object(m.sys, "platform", "linux"))
        if spec.get("hold"):
            if site.startswith("machine_") or site == "handback_accounting":
                stack.enter_context(mock.patch.object(m, "unlock", return_value=None))
            else:
                stack.enter_context(mock.patch.object(m, "released", keep_locked))
        if spec.get("unserialized"):
            stack.enter_context(mock.patch.object(m, "reservation", lambda *a, **kw: contextlib.nullcontext(True)))
        if spec.get("acquire"):
            real_released = m.released
            @contextlib.contextmanager
            def acquire_reader(inst, *a, **kw):
                with m.brain_lock(inst):
                    with real_released(inst, *a, **kw):
                        yield
            stack.enter_context(mock.patch.object(m, "released", acquire_reader))
        if spec.get("broad"):
            signature = m.state_signature
            def broad_signature(inst, node=None, extra=(), local_keys=(), **kw):
                nodes = inst.nodes() if node is None else ([node] if isinstance(node, dict) else list(node))
                paths = list(extra() if callable(extra) else extra)
                paths += [inst.local_path]
                paths += [p for n in nodes for p in n["dir"].iterdir()
                          if p.name not in ("nodes", "core", ".git", ".brain")]
                return signature(inst, node, extra=paths, local_keys=local_keys, **kw)
            stack.enter_context(mock.patch.object(m, "state_signature", broad_signature))
        if spec.get("no_lock"):
            stack.enter_context(mock.patch.object(m.brain_lock, "__enter__",
                                                 side_effect=AssertionError("reader acquired state lock")))
        if spec.get("blind"):
            stack.enter_context(mock.patch.object(m, "state_signature", return_value=b"blind"))
        if spec.get("stale"):
            real_released = m.released
            @contextlib.contextmanager
            def stale_bindings(inst, *args, **kw):
                old = json.loads(json.dumps(inst.local))
                with real_released(inst, *args, **kw):
                    yield
                inst.local = old
            stack.enter_context(mock.patch.object(m, "released", stale_bindings))
        if site == "job_accounting":
            inst = m.Instance(Path(spec["root"]))
            with m.brain_lock(inst):
                m.job_ended(inst, inst.node("app"), "job-one", "abandoned", cause="fixture")
        else:
            sys.argv = ["brain", *spec["command"]]
            m.main()
        if not fired:
            raise AssertionError("slow boundary was not reached")


class _LockSiteScenario(Scenario):
    def setUp(self):
        super().setUp()
        self.env["BRAIN_NO_UPDATE_CHECK"] = "1"
        self.env["BRAIN_LOCK_WAIT"] = "15"
        self.r = self.init("demo")
        self.home = self.repo(self.tmp / "application")
        self.b(self.r, "new", "app", "--title", "Application", "--home", str(self.home))
        self.b(self.r, "new", "other", "--title", "Other")
        self.marker, self.release = self.tmp / "slow-ready", self.tmp / "slow-release"

    def prepare(self, site):
        command = []
        if site.startswith(("sync_", "rm_", "prune_")) or site in ("job_accounting", "handback_accounting", "review_usage"):
            self.b(self.r, "wt", "add", "app", "job-one", "--base", "main")
            local = json.loads((self.r / ".brain/local.json").read_text())
            self.wt = Path(local["nodes"]["app"]["worktrees"]["job-one"]["path"])
        if site.startswith(("sync_", "prune_")) or site == "rm_prs":
            self.git("remote", "add", "origin", "https://github.com/Example/Fixture.git", cwd=self.home)
            tip = self.git("rev-parse", "main", cwd=self.home).strip()
            self.git("update-ref", "refs/remotes/origin/main", tip, cwd=self.home)
            self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=self.home)
        if site in ("rm_prs", "prune_prs", "prune_accounting"):
            (self.wt / "change.txt").write_text("fixture change\n")
            self.git("add", "-A", cwd=self.wt)
            self.git("commit", "-qm", "fixture job", cwd=self.wt)
            if site == "prune_accounting":
                self.git("merge", "--ff-only", "job-one", cwd=self.home)
        if site.startswith("sync_"):
            command = ["sync", "app", "--mark"]
        elif site.startswith("rm_"):
            command = ["wt", "rm", "app", "job-one", "--cause", "fixture"]
        elif site.startswith("prune_"):
            command = ["wt", "prune", "app"]
        elif site == "study_accounting":
            branch = "import:" + self.git("rev-parse", "HEAD", cwd=self.home).strip()
            self.b(self.r, "jobs", "app", "--branch", branch, "--start")
            command = ["jobs", "app", "--branch", branch, "--close"]
        elif site == "handback_accounting":
            command = ["handback", "app", "--branch", "job-one", "--state", "blocked", "--summary", "Fixture result", "--lessons", "none: fixture"]
        elif site.startswith("spend_"):
            command = ["spend", "--machine"] if site == "spend_machine" else ["spend", "app"]
        elif site == "history_out":
            sessions = self.tmp / ".codex/sessions"
            sessions.mkdir(parents=True)
            rows = [
                {"type": "session_meta", "payload": {"id": "a0000001-fixture", "cwd": str(self.home)}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "Use a small queue."}},
                {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "Queued."}},
            ]
            (sessions / "rollout-fixture.jsonl").write_text("".join(json.dumps({"timestamp": "2026-01-01T10:00:00Z", **x}) + "\n" for x in rows))
            self.b(self.r, "history", "app")
            command = ["history", "app", "brief", "b001", "--out", str(self.tmp / "history-brief.md")]
        elif site.startswith("connect_"):
            for nid in ("hub", "hub/a", "hub/b"):
                self.b(self.r, "new", nid, "--title", nid)
            for child in ("a", "b"):
                (self.r / "nodes/hub/nodes" / child / "knowledge.md").write_text("# Knowledge\n\nUse a small queue.\n")
            command = ["connect", "hub"]
            if site == "connect_plan":
                command += ["plan", "--fresh"]
            if site in ("connect_save", "connect_existing_plan"):
                self.b(self.r, "connect", "hub")
                command += ["brief", "hub/a", "--save"] if site == "connect_save" else ["plan"]
        elif site.endswith("_usage"):
            config = self.tmp / ".config/brain"
            config.mkdir(parents=True, exist_ok=True)
            (config / "subscriptions.json").write_text(json.dumps({"subscriptions": [{
                "id": "fixture", "runtime": "codex", "config_dir": str(self.tmp / ".codex"),
                "host_provider_instance_id": "fixture-host", "brains": ["*"]}]}))
            if site == "brief_usage":
                command = ["brief", "app", "--task", "Check the queue.", "--out", str(self.tmp / "brief.md")]
            elif site == "review_usage":
                command = ["review", "app", "--branch", "job-one", "--built-by", "claude", "--out", str(self.tmp / "review.md")]
            else:
                self.b(self.r, "specialist", "new", "helper", "--title", "Helper")
                command = ["hire", "helper", "app", "--task", "Check the queue.", "--job", "fixture"]
        elif site.startswith("machine_"):
            command = ["machine", site.split("_", 1)[1], "--yes"]
        return command

    def start(self, site, command, hold=False, gated=False, blind=False, stale=False, wait=True, unserialized=False, timeout=False, acquire=False, broad=False, no_lock=False):
        spec = self.tmp / "worker.json"
        spec.write_text(json.dumps({"site": site, "command": command, "root": str(self.r),
                                    "marker": str(self.marker), "hold": hold, "blind": blind, "stale": stale, "unserialized": unserialized, "timeout": timeout,
                                    "acquire": acquire, "broad": broad, "no_lock": no_lock,
                                    "release": str(self.release) if gated else ""}))
        env = {**self.env, "BRAIN_ROOT": str(self.r)}
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--lock-worker", str(spec)],
                                cwd=self.r, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        self.addCleanup(self.stop, proc)
        if not wait:
            return proc
        deadline = time.monotonic() + 15
        while not self.marker.exists():
            if proc.poll() is not None:
                out, err = proc.communicate()
                self.fail("worker exited before slow boundary:\n" + out + err)
            if time.monotonic() > deadline:
                self.fail("worker did not enter the slow boundary")
            time.sleep(0.02)
        return proc

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
        proc.communicate()

    def sibling_probes(self):
        times = {}
        for name, args in (("status", ["status", "other", "--refresh"]),
                           ("set", ["set", "other", "preview=http://127.0.0.1:4999"]),
                           ("due", ["due", "other"])):
            start = time.monotonic()
            result = subprocess.run([sys.executable, str(self.r / "core/bin/brain"), *args],
                                    cwd=self.r, env=self.env, capture_output=True, text=True, timeout=5)
            times[name] = time.monotonic() - start
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(times[name], 5)
        return times

    def responsive(self, site):
        command = self.prepare(site)
        start = time.monotonic()
        proc = self.start(site, command, gated=True)
        times = self.sibling_probes()
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, out + err)
        local = json.loads((self.r / ".brain/local.json").read_text())
        self.assertEqual(local["nodes"]["other"]["preview"], "http://127.0.0.1:4999")
        # Positive publication assertions prevent a fast no-op from passing.
        if site.startswith("sync_"):
            self.assertTrue(local["nodes"]["app"].get("synced_at"))
        if site in ("rm_prs", "rm_accounting", "prune_accounting", "job_accounting"):
            self.assertIn("abandoned" if site != "prune_accounting" else "merged",
                          (self.r / "nodes/app/job-ledger.md").read_text())
        if site == "study_accounting":
            self.assertIn("studied", (self.r / "nodes/app/job-ledger.md").read_text())
        if site == "history_out":
            self.assertTrue((self.tmp / "history-brief.md").is_file())
        if site in ("connect_default", "connect_plan", "connect_existing_plan"):
            self.assertTrue((self.r / ".brain/connect/hub/plan.json").is_file())
        if site == "connect_save":
            self.assertTrue(list((self.r / ".brain/connect/hub/briefs").glob("*.md")))
        if site == "hire_usage":
            self.assertEqual(local["hires"][-1]["job"], "fixture")
        if site == "spend_node":
            self.assertTrue((self.r / ".brain/spend/nodes/app.json").is_file())
        if site == "handback_accounting":
            records = list((self.r / "nodes/app/job-handbacks").glob("*.json"))
            self.assertEqual(len(records), 1)
            self.assertEqual(json.loads(records[0].read_text())["state"], "blocked")
        if site.startswith("machine_"):
            self.assertEqual(Path(str(self.marker) + ".mutation").read_text(), site)
        if os.environ.get("BRAIN_LOCK_TIMINGS"):
            with open(os.environ["BRAIN_LOCK_TIMINGS"], "a") as fh:
                fh.write(json.dumps({"site": site, **times}) + "\n")

    def held_control(self, site):
        command = self.prepare(site)
        import fcntl
        proc = self.start(site, command, hold=True, gated=True)
        with (self.r / ".brain/lock").open("r") as held:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertIsNone(proc.poll())
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, out + err)


class ReleasedSites(_LockSiteScenario):
    fixture_init_in_process = True
    fixture_templates = True

    def setUp(self):
        Scenario.setUp(self)
        self.env['BRAIN_NO_UPDATE_CHECK'] = '1'
        self.env['BRAIN_LOCK_WAIT'] = '15'
        self.r, self.home = self.cached_setup('released-site', self.site_fixture)
        self.marker, self.release = self.tmp / 'slow-ready', self.tmp / 'slow-release'

    def site_fixture(self):
        self.r = self.init('demo')
        self.home = self.repo(self.tmp / 'application')
        self.b(self.r, 'new', 'app', '--title', 'Application', '--home', str(self.home))
        self.b(self.r, 'new', 'other', '--title', 'Other')
        return self.r, self.home


# Each case is independently runnable, with its own synthetic instance.
for _site in SITES:
    def positive(self, site=_site):
        self.responsive(site)
    def negative(self, site=_site):
        self.held_control(site)
    setattr(ReleasedSites, "test_" + _site + "_responsive", positive)
    setattr(ReleasedSites, "test_" + _site + "_hold_mutant", negative)


class RetakeConflicts(_LockSiteScenario):
    def conflict(self, site):
        command = self.prepare(site)
        keep = None
        if site in ("connect_default", "connect_plan"):
            folder = self.r / ".brain/connect/hub"
            folder.mkdir(parents=True, exist_ok=True)
            keep = folder / "keep.txt"
            keep.write_text("keep previous output")
        ledger = self.r / "nodes/app/job-ledger.md"
        before = ledger.read_bytes() if ledger.exists() else None
        proc = self.start(site, command, gated=True)
        if site.startswith("connect_"):
            target = (self.r / ".brain/connect/hub/plan.json" if site in ("connect_save", "connect_existing_plan") else
                      self.r / "nodes/hub/nodes/a/knowledge.md")
            target.write_text(target.read_text() + "\n")
        elif site.endswith("_usage"):
            target = self.tmp / ".config/brain/subscriptions.json"
            target.write_text(target.read_text() + "\n")
        elif site == "spend_machine":
            (self.r / "never-name.txt").write_text("FabricatedSensitiveTerm\n")
        else:
            self.b(self.r, "set", "app", "preview=http://127.0.0.1:4888")
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertNotEqual(proc.returncode, 0, out + err)
        self.assertIn("state changed during the unlocked step", err)
        if before is not None:
            self.assertEqual(ledger.read_bytes(), before)
        if keep:
            self.assertEqual(keep.read_text(), "keep previous output")
            self.assertFalse((keep.parent / "plan.json").exists())
        if site == "history_out":
            self.assertFalse((self.tmp / "history-brief.md").exists())
        if site in ("brief_usage", "review_usage"):
            self.assertFalse((self.tmp / ("brief.md" if site == "brief_usage" else "review.md")).exists())
        if site == "hire_usage":
            self.assertNotIn("hires", json.loads((self.r / ".brain/local.json").read_text()))
        if site in ("rm_prs", "rm_accounting", "prune_accounting"):
            self.assertTrue(self.wt.is_dir())

    def test_refresh_detects_inherited_specialist_change(self):
        command = self.prepare("hire_usage")
        proc = self.start("hire_usage", command, gated=True)
        role = self.r / "specialists/helper/role.md"
        role.write_text(role.read_text().replace("status: active", "status: proposed") + "\n")
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertNotEqual(proc.returncode, 0, out + err)
        self.assertIn("state changed during the unlocked step", err)
        self.assertNotIn("hires", json.loads((self.r / ".brain/local.json").read_text()))


for _site in SITES:
    if _site.startswith("machine_") or _site == "handback_accounting":
        continue  # Services serialize independently; handback is already published.
    def conflict(self, site=_site):
        self.conflict(site)
    setattr(RetakeConflicts, "test_" + _site + "_conflict", conflict)


class ReleasedMechanics(_LockSiteScenario):
    def module(self):
        return core_loader.SourceFileLoader("lock_mechanics", str(CORE / "bin/brain")).load_module()

    def test_released_preserves_nested_lock_depth_and_checks_same_size_edits(self):
        m = self.module()
        inst = m.Instance(self.r)
        policy = self.r / "nodes/app/never-name.txt"
        policy.write_text("Alpha\n")
        with mock.patch.dict(os.environ, self.env, clear=True):
            with m.brain_lock(inst):
                with m.brain_lock(inst):
                    depth = m._LOCK["depth"]
                    with m.released(inst, inst.node("app")):
                        self.assertIsNone(m._LOCK["fh"])
                    self.assertEqual(m._LOCK["depth"], depth)
                    self.assertIsNotNone(m._LOCK["fh"])
                    original = policy.stat()
                    with self.assertRaisesRegex(SystemExit, "state changed"):
                        with m.released(inst, inst.node("app")):
                            policy.write_text("Bravo\n")
                            os.utime(policy, ns=(original.st_atime_ns, original.st_mtime_ns))
                    self.assertIsNotNone(m._LOCK["fh"])
            self.assertEqual(m._LOCK["depth"], 0)
            self.assertIsNone(m._LOCK["fh"])

    def test_removed_target_refuses_retake_without_a_path_traceback(self):
        m = self.module()
        inst = m.Instance(self.r)
        node = inst.node("app")
        with mock.patch.dict(os.environ, self.env, clear=True):
            with m.brain_lock(inst):
                with self.assertRaisesRegex(SystemExit, "state changed during the unlocked step"):
                    with m.released(inst, node):
                        shutil.rmtree(node["dir"])
                self.assertIsNotNone(m._LOCK["fh"])
            self.assertIsNone(m._LOCK["fh"])

    def test_nested_release_returns_to_the_outer_unlocked_phase(self):
        m = self.module()
        inst = m.Instance(self.r)
        with mock.patch.dict(os.environ, self.env, clear=True):
            with m.brain_lock(inst):
                with m.released(inst, inst.node("app")):
                    with m.released(inst, inst.node("app")):
                        self.assertIsNone(m._LOCK["fh"])
                    self.assertIsNone(m._LOCK["fh"])
                self.assertIsNotNone(m._LOCK["fh"])
            self.assertIsNone(m._LOCK["fh"])

    def test_signature_does_not_read_source_or_manifest_bodies(self):
        m = self.module()
        inst = m.Instance(self.r)
        source = self.r / "nodes/app/sources"
        source.mkdir(exist_ok=True)
        (source / "large.txt").write_text("large synthetic source\n" * 10000)
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("body read under state lock")):
            m.state_signature(inst, inst.node("app"))

    def test_release_keeps_original_exception_and_does_not_publish(self):
        m = self.module()
        inst = m.Instance(self.r)
        with mock.patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(ValueError, "original failure"):
                with m.released(inst, inst.node("app")):
                    raise ValueError("original failure")
            self.assertIsNone(m._LOCK["fh"])
            self.assertEqual(m._LOCK["depth"], 0)

    def test_disabling_retake_guard_accepts_a_conflicting_usage_policy(self):
        command = self.prepare("brief_usage")
        proc = self.start("brief_usage", command, gated=True, blind=True)
        policy = self.tmp / ".config/brain/subscriptions.json"
        policy.write_text(policy.read_text() + "\n")
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertTrue((self.tmp / "brief.md").is_file())

    def test_disabling_binding_reload_loses_the_sibling_binding(self):
        command = self.prepare("sync_fetch")
        proc = self.start("sync_fetch", command, gated=True, stale=True)
        self.b(self.r, "set", "other", "preview=http://127.0.0.1:4777")
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertNotIn("preview", json.loads((self.r / ".brain/local.json").read_text())["nodes"].get("other", {}))

    def test_timeout_retake_preserves_the_sibling_binding(self):
        command = self.prepare("sync_prs")
        proc = self.start("sync_prs", command, gated=True, timeout=True)
        self.b(self.r, "set", "other", "preview=http://127.0.0.1:4666")
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, out + err)
        local = json.loads((self.r / ".brain/local.json").read_text())
        self.assertEqual(local["nodes"]["other"]["preview"], "http://127.0.0.1:4666")
        self.assertTrue(local["nodes"]["app"]["synced_at"])

    def test_timeout_retake_refuses_a_conflicting_target(self):
        command = self.prepare("sync_prs")
        proc = self.start("sync_prs", command, gated=True, timeout=True)
        self.b(self.r, "set", "app", "preview=http://127.0.0.1:4555")
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertNotEqual(proc.returncode, 0, out + err)
        self.assertIn("state changed", err)
        local = json.loads((self.r / ".brain/local.json").read_text())
        self.assertEqual(local["nodes"]["app"]["preview"], "http://127.0.0.1:4555")
        self.assertNotIn("synced_at", local["nodes"]["app"])

    def test_accounting_refuses_a_restarted_job(self):
        command = self.prepare("job_accounting")
        proc = self.start("job_accounting", command, gated=True)
        m = self.module()
        inst = m.Instance(self.r)
        with m.brain_lock(inst):
            node = inst.node("app")
            rows = m.read_jobs(node)
            rows[-1]["Started"] = "2030-01-01T00:00:00+00:00"
            m.write_jobs(inst, node, rows)
        before = (self.r / "nodes/app/job-ledger.md").read_bytes()
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertNotEqual(proc.returncode, 0, out + err)
        self.assertIn("job changed during accounting", err)
        self.assertEqual((self.r / "nodes/app/job-ledger.md").read_bytes(), before)

    def test_accounting_refuses_changed_subscription_policy(self):
        command = self.prepare("job_accounting")
        policy = self.tmp / ".config/brain/subscriptions.json"
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text('{"subscriptions": []}\n')
        before = (self.r / "nodes/app/job-ledger.md").read_bytes()
        proc = self.start("job_accounting", command, gated=True)
        policy.write_text('{"subscriptions": [], "stop_percent": 80}\n')
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertNotEqual(proc.returncode, 0, out + err)
        self.assertIn("state changed", err)
        self.assertEqual((self.r / "nodes/app/job-ledger.md").read_bytes(), before)

    def test_history_refuses_to_replace_a_changed_output(self):
        command = self.prepare("history_out")
        destination = self.tmp / "history-brief.md"
        destination.write_text("previous output")
        proc = self.start("history_out", command, gated=True)
        destination.write_text("concurrent output")
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertNotEqual(proc.returncode, 0, out + err)
        self.assertIn("state changed", err)
        self.assertEqual(destination.read_text(), "concurrent output")

    def service_pair(self, unserialized=False):
        command = self.prepare("machine_install")
        first = self.start("machine_install", command, gated=True, unserialized=unserialized)
        first_release = self.release
        preview = subprocess.run([sys.executable, str(self.r / "core/bin/brain"), "machine", "install"],
                                 cwd=self.r, env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn("dry run", preview.stdout)
        self.marker, self.release = self.tmp / "second-ready", self.tmp / "second-release"
        command = self.prepare("machine_uninstall")
        second = self.start("machine_uninstall", command, gated=True, wait=False, unserialized=unserialized)
        deadline = time.monotonic() + (5 if unserialized else 0.5)
        while not self.marker.exists() and time.monotonic() < deadline:
            self.assertIsNone(second.poll())
            time.sleep(0.02)
        self.assertEqual(self.marker.exists(), unserialized)
        self.sibling_probes()  # A waiting service operation never takes state.
        first_release.write_text("release")
        out, err = first.communicate(timeout=15)
        self.assertEqual(first.returncode, 0, out + err)
        deadline = time.monotonic() + 10
        while not self.marker.exists() and time.monotonic() < deadline:
            self.assertIsNone(second.poll())
            time.sleep(0.02)
        self.assertTrue(self.marker.exists())
        self.release.write_text("release")
        out, err = second.communicate(timeout=15)
        self.assertEqual(second.returncode, 0, out + err)

    def test_machine_service_operations_serialize_without_state(self):
        self.service_pair()

    def test_disabling_service_reservation_interleaves_the_services(self):
        self.service_pair(unserialized=True)

    def test_refresh_detects_changed_shared_context_before_output(self):
        command = self.prepare("brief_usage")
        context = self.tmp / "shared-context.md"
        context.write_text("The fixture queue is small.\n")
        command += ["--context-file", str(context)]
        proc = self.start("brief_usage", command, gated=True)
        context.write_text("The fixture queue has changed.\n")
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertNotEqual(proc.returncode, 0, out + err)
        self.assertIn("state changed during the unlocked step", err)
        self.assertFalse((self.tmp / "brief.md").exists())


class ReviewReaders(_LockSiteScenario):
    def reader(self, name, mutant=False):
        if name == "usage":
            self.prepare("brief_usage")
            site, command = "usage_reader", ["usage", "--refresh"]
        elif name == "history":
            site, command = "history_reader", self.prepare("history_out")[:-2]
        elif name == "connect":
            site, command = "connect_reader", self.prepare("connect_save")[:-1]
        elif name == "brief":
            site, command = "brief_usage", self.prepare("brief_usage")[:-2]
        else:
            site, command = "hire_usage", self.prepare("hire_usage")[:-2]
        # Every reader must reach its slow boundary while another process owns state.
        import fcntl
        with open(self.r / ".brain/lock", "a+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            proc = self.start(site, command, gated=True, acquire=mutant, no_lock=True, wait=False)
            deadline = time.monotonic() + 5
            while not self.marker.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if not mutant:
                if not self.marker.exists():
                    self.fail(str(proc.communicate(timeout=1)))
            else:
                out, err = proc.communicate(timeout=5)
                self.assertNotEqual(proc.returncode, 0, out + err)
                self.assertIn("reader acquired state lock", err)
                return
        self.b(self.r, "set", "other", "preview=http://127.0.0.1:4333")
        (self.r / "nodes/other/status.md").write_text("Concurrent sibling work.\n")
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertTrue(out.strip())
        self.assertNotIn("hires", json.loads((self.r / ".brain/local.json").read_text()))

    def test_history_stdout_finishes_while_writer_holds_lock(self):
        command = self.prepare("history_out")[:-2]
        import fcntl
        with open(self.r / ".brain/lock", "a+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            started = time.monotonic()
            r = subprocess.run([sys.executable, str(self.r / "core/bin/brain"), *command],
                               cwd=self.r, env=self.env, capture_output=True, text=True, timeout=1)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertLess(time.monotonic() - started, 1)


for _reader in ("usage", "history", "connect", "brief", "hire"):
    def reader(self, name=_reader):
        self.reader(name)
    def mutation(self, name=_reader):
        self.reader(name, mutant=True)
    setattr(ReviewReaders, "test_" + _reader + "_never_acquires", reader)
    setattr(ReviewReaders, "test_" + _reader + "_acquire_mutant", mutation)


class ReviewReadSets(_LockSiteScenario):
    module = ReleasedMechanics.module
    def unrelated(self, site, mutant=False):
        command = self.prepare(site)
        if site == "rm_accounting":
            self.b(self.r, "wt", "add", "app", "job-two", "--base", "main")
        proc = self.start(site, command, gated=True, broad=mutant)
        if site == "rm_accounting":
            self.b(self.r, "handback", "app", "--branch", "job-two", "--state", "blocked", "--summary", "Fixture result", "--lessons", "none: fixture")
            target = self.r / "nodes/app/inbox.md"
            target.write_text(target.read_text() + "\nUnrelated inbox message.\n")
            records = list((self.r / "nodes/app/job-handbacks").glob("*.json"))
            self.assertEqual(len(records), 1)
            record = records[0].read_bytes()
        elif site == "sync_fetch":
            self.b(self.r, "changes", "other", "--seen")
            seen = json.loads((self.r / ".brain/local.json").read_text())["changes_seen"]
        else:
            target = self.r / "nodes/hub/nodes/b/inbox.md"
            target.write_text(target.read_text() + "\nUnrelated inbox message.\n")
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        if mutant:
            self.assertNotEqual(proc.returncode, 0, out + err)
            self.assertIn("state changed", err)
            return
        self.assertEqual(proc.returncode, 0, out + err)
        if site == "rm_accounting":
            self.assertFalse(self.wt.exists())
            self.assertIn("abandoned", (self.r / "nodes/app/job-ledger.md").read_text())
            self.assertEqual(records[0].read_bytes(), record)
        elif site == "sync_fetch":
            local = json.loads((self.r / ".brain/local.json").read_text())
            self.assertTrue(local["nodes"]["app"]["synced_at"])
            self.assertEqual(local["changes_seen"], seen)
        else:
            self.assertTrue((self.r / ".brain/connect/hub/plan.json").is_file())

    def test_accounting_preserves_concurrent_host_usage(self):
        command = self.prepare("job_accounting")
        proc = self.start("job_accounting", command, gated=True)
        m = self.module()
        inst = m.Instance(self.r)
        with m.brain_lock(inst):
            n = inst.node("app")
            rows = m.read_jobs(n)
            rows[-1].update(Usage="host: fixture", Tokens="12345")
            m.write_jobs(inst, n, rows)
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, out + err)
        row = m.read_jobs(inst.node("app"))[-1]
        self.assertEqual(row["Usage"], "host: fixture")
        self.assertEqual(row["Tokens"], "12345")
        self.assertTrue(row["Ended"])

    def test_machine_reservation_times_out(self):
        self.env["BRAIN_LOCK_WAIT"] = "1"
        import fcntl
        with open(self.r / ".brain/machine-service.lock", "a+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            started = time.monotonic()
            r = subprocess.run([sys.executable, str(self.r / "core/bin/brain"), "machine", "install", "--yes"],
                               cwd=self.r, env=self.env, capture_output=True, text=True, timeout=3)
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("another brain command has held this operation's lock", r.stderr)
            self.assertLess(time.monotonic() - started, 3)

    def test_history_nonbrief_out_refuses_without_writing(self):
        destination = self.tmp / "ignored.md"
        for action in ([], ["plan"]):
            out = self.b(self.r, "history", "app", *action, "--out", str(destination), ok=False)
            self.assertIn("--out goes with history brief", out)
            self.assertFalse(destination.exists())
            self.assertFalse((self.r / "nodes/app/history").exists())

    def test_accounting_refuses_restarted_job_before_removal(self):
        for site in ("rm_accounting", "prune_accounting"):
            with self.subTest(site=site):
                # Each destructive path needs a fresh fixture and rendezvous.
                case = _LockSiteScenario()
                case.setUp()
                try:
                    command = case.prepare(site)
                    proc = case.start(site, command, gated=True)
                    m = self.module()
                    inst = m.Instance(case.r)
                    with m.brain_lock(inst):
                        n = inst.node("app")
                        rows = m.read_jobs(n)
                        rows[-1]["Started"] = "2030-01-01T00:00:00+00:00"
                        m.write_jobs(inst, n, rows)
                    before = (case.r / "nodes/app/job-ledger.md").read_bytes()
                    case.release.write_text("release")
                    out, err = proc.communicate(timeout=15)
                    self.assertNotEqual(proc.returncode, 0, out + err)
                    self.assertIn("job changed during accounting", err)
                    self.assertTrue(case.wt.is_dir())
                    self.assertEqual((case.r / "nodes/app/job-ledger.md").read_bytes(), before)
                finally:
                    case.doCleanups()
                    case.tearDown()

    def test_signature_ignores_unrelated_records_and_local_settings(self):
        m = self.module()
        inst = m.Instance(self.r)
        node = inst.node("app")
        before = m.state_signature(inst, node)
        (node["dir"] / "inbox.md").write_text("Unrelated inbox message.\n")
        (node["dir"] / "status.md").write_text("Unrelated status.\n")
        local = inst._load_local()
        local.update(changes_seen={"other": "seen"}, hires=[{"job": "other"}], schedules={"other": []})
        local["nodes"]["other"] = {"preview": "http://127.0.0.1:4000"}
        inst.local_path.write_text(json.dumps(local))
        self.assertEqual(m.state_signature(inst, node), before)
        before = m.state_signature(inst, node, local_keys=("fallback",))
        local["fallback"] = ["fixture-host"]
        inst.local_path.write_text(json.dumps(local))
        self.assertNotEqual(m.state_signature(inst, node, local_keys=("fallback",)), before)

    def test_reader_release_does_not_sign_validate_or_reload(self):
        m = self.module()
        inst = m.Instance(self.r)
        node = inst.node("app")
        local = inst.local
        with mock.patch.object(m, "state_signature", side_effect=AssertionError("reader signed")):
            with mock.patch.object(inst, "_load_local", side_effect=AssertionError("reader reloaded")):
                with m.released(inst, node):
                    self.assertIsNone(m._LOCK["fh"])
        self.assertIs(inst.local, local)

    def test_history_writer_ignores_other_batch_notes_but_validates_manifest(self):
        command = self.prepare("history_out")
        m = self.module()
        inst = m.Instance(self.r)
        d = m.history_dir(inst, inst.node("app"))[1]
        (d / "notes").mkdir(exist_ok=True)
        proc = self.start("history_out", command, gated=True)
        (d / "notes/b999.md").write_text("Unrelated reader's notes.\n")
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertTrue((self.tmp / "history-brief.md").is_file())
        self.marker.unlink()
        self.release.unlink()
        proc = self.start("history_out", command, gated=True)
        manifest = d / "manifest.json"
        manifest.write_text(manifest.read_text() + "\n")
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertNotEqual(proc.returncode, 0, out + err)
        self.assertIn("state changed", err)

    def test_hire_writer_ignores_another_specialist(self):
        command = self.prepare("hire_usage")
        self.b(self.r, "specialist", "new", "other-helper", "--title", "Other helper")
        proc = self.start("hire_usage", command, gated=True)
        role = self.r / "specialists/other-helper/role.md"
        role.write_text(role.read_text() + "\nUnrelated specialist.\n")
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertEqual(json.loads((self.r / ".brain/local.json").read_text())["hires"][-1]["job"], "fixture")

    def test_brief_reloads_contract_after_usage(self):
        command = self.prepare("review_usage")  # supplies an open worktree job
        self.prepare("brief_usage")  # supplies stale usage configuration
        command = ["brief", "app", "--branch", "job-one", "--out", str(self.tmp / "brief.md")]
        m = self.module()
        inst = m.Instance(self.r)
        n = inst.node("app")
        row = m.open_job(m.read_jobs(n), "job-one")
        contract = m.contract_file(n, row)
        contract.parent.mkdir(exist_ok=True)
        contract.write_text("## Why separate\n\nOriginal reason.\n")
        proc = self.start("brief_usage", command, gated=True)
        contract.write_text("## Why separate\n\nCurrent reason.\n")
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, out + err)
        text = (self.tmp / "brief.md").read_text()
        self.assertIn("Why a separate agent: Current reason.", text)
        self.assertNotIn("Original reason.", text)

    def test_accounting_refuses_changed_own_handback(self):
        command = self.prepare("rm_accounting")
        m = self.module()
        inst = m.Instance(self.r)
        n = inst.node("app")
        row = m.open_job(m.read_jobs(n), "job-one")
        record = m.handback_file(n, row)
        proc = self.start("rm_accounting", command, gated=True)
        record.parent.mkdir(exist_ok=True)
        record.write_text('{"sessions": ["a0000001-fixture"]}\n')
        self.release.write_text("release")
        out, err = proc.communicate(timeout=15)
        self.assertNotEqual(proc.returncode, 0, out + err)
        self.assertIn("state changed", err)
        self.assertTrue(self.wt.is_dir())


for _site in ("rm_accounting", "sync_fetch", "connect_plan"):
    def unrelated(self, site=_site):
        self.unrelated(site)
    def broad(self, site=_site):
        self.unrelated(site, mutant=True)
    setattr(ReviewReadSets, "test_" + _site + "_unrelated", unrelated)
    setattr(ReviewReadSets, "test_" + _site + "_broad_mutant", broad)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--lock-worker":
        lock_worker(sys.argv[2])
    else:
        unittest.main()
