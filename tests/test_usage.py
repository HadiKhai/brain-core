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
from zoneinfo import ZoneInfo

CORE = Path(os.environ.get("BRAIN_CORE") or Path(__file__).resolve().parent.parent)


from scenario import Scenario, AutonomyProject, shlex_quote


class Usage(Scenario):
    def setUp(self):
        super().setUp()
        self.cfg = self.tmp / "config"
        self.cfg.mkdir()
        self.env["BRAIN_CONFIG"] = str(self.cfg / "brains.json")
        self.patch_env = mock.patch.dict(os.environ, self.env, clear=True)
        self.patch_env.start()
        self.addCleanup(self.patch_env.stop)
        loader = core_loader.SourceFileLoader("brain_usage_test", str(CORE / "bin/brain"))
        self.m = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.m)

    def subscription(self, id="one", runtime="codex", brains=None):
        cfg = self.tmp / ("runtime-" + id)
        cfg.mkdir(exist_ok=True)
        return {"id": id, "runtime": runtime, "config_dir": str(cfg),
                "host_provider_instance_id": "host-" + id, "brains": brains if brains is not None else ["alice"]}

    def declare(self, *subs, **policy):
        (self.cfg / "subscriptions.json").write_text(json.dumps({"subscriptions": subs, **policy}))
        return self.m.usage_config()

    def fixture(self, sub, used=24, account="same-account"):
        reset = int(time.time() + 3 * 86400)
        data = {"account": {"accountId": account}, "rateLimits": {"limitId": "codex", "primary": {
            "usedPercent": used, "windowDurationMins": 10080, "resetsAt": reset}, "secondary": None}}
        (Path(sub["config_dir"]) / "fixture.json").write_text(json.dumps(data))
        return data

    def codex_stub(self):
        folder = self.tmp / "bin"
        folder.mkdir(exist_ok=True)
        exe = folder / "codex"
        exe.write_text("#!" + sys.executable + "\n" + r'''import json,os,sys
from pathlib import Path
cfg=Path(os.environ['CODEX_HOME'])
for line in sys.stdin:
    msg=json.loads(line)
    with (cfg/'requests.txt').open('a') as f: f.write(msg['method']+'\n')
    if 'id' not in msg: continue
    data=json.loads((cfg/'fixture.json').read_text())
    method=msg['method']
    if method=='initialize': result={}
    elif method=='account/read': result={'account':data['account']}
    elif method=='account/rateLimits/read': result={'rateLimits':data['rateLimits']}
    else: sys.exit(4)
    print(json.dumps({'method':'notification','params':{}}),flush=True)
    print(json.dumps({'id':msg['id'],'result':result}),flush=True)
''')
        exe.chmod(0o755)
        self.env["PATH"] = str(folder) + ":" + self.env["PATH"]
        os.environ["PATH"] = self.env["PATH"]

    def cache(self, config, readings, checked=None):
        subs, accounts = [], {}
        for sub, reading in zip(config["subscriptions"], readings):
            key = "key-" + sub["id"]
            subs.append({**sub, "account_key": key})
            accounts[key] = reading
        data = {**config, "subscriptions": subs, "accounts": accounts,
                "checked_at": time.time() if checked is None else checked}
        self.m.usage_atomic(self.m.usage_file(), data)
        return data

    def reading(self, used=20, weekly=None, elapsed=50, source="lookup", age=0):
        windows = {"five_hour": {"used_percent": used, "resets_at": time.time() + 3600}}
        if weekly is not None:
            windows["seven_day"] = {"used_percent": weekly,
                                    "resets_at": time.time() + (1 - elapsed / 100) * 7 * 86400}
        return {"windows": windows if source != "unknown" else {}, "source": source,
                "read_at": time.time() - age if source != "unknown" else None, "note": ""}

    def test_builders_keep_codex_at_65_and_stop_line(self):
        root = self.init("alice")
        inst = self.m.Instance(root)
        config = self.declare(self.subscription("cx"), self.subscription("cl", runtime="claude"))
        for used in (65, 90):
            self.cache(config, [self.reading(10, weekly=used), self.reading(10, weekly=20)])
            for kind in ("build", "ui", "migration", "docs", "mechanical"):
                runtime, sub, why = self.m.class_route(inst, kind, weekly_cap=1, cached_only=True)
                self.assertEqual(runtime, "codex")
                self.assertEqual(sub["id"], "cx")
                self.assertIn(f"Weekly meter {used}%; keeping codex", why)
                self.assertIn("Builder policy", why)
                if used == 90:
                    self.assertIn("stop line", why)
                    self.assertIn("Alternative: cl (claude)", why)
                    self.assertIn("choose --runtime explicitly", why)
            self.assertEqual(self.m.class_route(inst, "review", cached_only=True)[0], "claude")
            self.assertEqual(self.m.class_route(inst, "build", runtime="claude", cached_only=True)[0], "claude")
        for kind in (None, *self.m.JOB_CLASSES):
            self.assertFalse(self.m.class_allows_fallback(kind))

    def test_class_effort_mappings(self):
        root = self.init("alice")
        node = self.m.Instance(root).node("root")
        for kind, expected in (("build", "high"), ("ui", "high"), ("migration", "high"),
                               ("docs", "medium"), ("mechanical", "medium"),
                               ("review", "high"), ("diagnose", "xhigh")):
            with self.subTest(kind=kind):
                self.assertEqual(self.m.job_effort(node, None, kind), expected)

    def test_effort_overrides_ledger_and_exact_launch_selection(self):
        root = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(root, "new", "a", "--title", "A", "--home", str(home))
        self.b(root, "set", "a", "runtime=", "route_model_codex=configured-codex", "route_model_claude=configured-claude")
        self.b(root, "wt", "add", "a", "job")
        inst = self.m.Instance(root)
        node = inst.node("a")
        row = self.m.open_job(self.m.read_jobs(node), "job")
        contract = self.m.contract_file(node, row)
        contract.parent.mkdir(exist_ok=True)
        contract.write_text("## Class\nbuild\n## Effort\nmedium\n")
        config = self.declare(self.subscription("cx"), self.subscription("cl", runtime="claude"))
        self.cache(config, [self.reading(10, weekly=65), self.reading(10, weekly=20)])
        def selection(text):
            return json.loads(next(line.split(": ", 1)[1] for line in text.splitlines()
                                   if line.startswith("modelSelection: {")))
        text = self.b(root, "brief", "a", "--branch", "job")
        self.assertEqual(selection(text), {"instanceId": "host-cx", "model": "configured-codex",
                                         "options": {"reasoningEffort": "medium"}})
        row = self.m.open_job(self.m.read_jobs(node), "job")
        self.assertEqual((row["Model"], row["Effort"]), ("configured-codex", "medium"))
        text = self.b(root, "brief", "a", "--branch", "job", "--effort", "xhigh")
        self.assertEqual(selection(text)["options"]["reasoningEffort"], "xhigh")
        self.assertEqual(self.m.open_job(self.m.read_jobs(node), "job")["Effort"], "xhigh")
        self.b(root, "specialist", "new", "qa", "--title", "QA")
        text = self.b(root, "hire", "qa", "a", "--branch", "job", "--class", "docs", "--task", "x", "--effort", "low")
        self.assertEqual(selection(text)["options"]["reasoningEffort"], "low")
        self.assertEqual(self.m.open_job(self.m.read_jobs(node), "job")["Effort"], "low")
        text = self.b(root, "review", "a", "--branch", "job", "--built-by", "codex", "--effort", "high")
        self.assertEqual(selection(text), {"instanceId": "host-cl", "model": "configured-claude",
                                         "options": {"reasoningEffort": "high"}})
        self.assertEqual(self.m.open_job(self.m.read_jobs(node), "job")["Review effort"], "high")
        contract.write_text("## Class\nbuild\n## Effort\ninvalid\n")
        out = self.b(root, "brief", "a", "--branch", "job", ok=False)
        self.assertIn(contract.name, out)
        self.assertIn("## Effort", out)
        self.b(root, "brief", "a", "--effort", "invalid", ok=False)
        # Missing model config is explicit; never fabricate an exact host model.
        self.b(root, "set", "a", "route_model_codex=")
        self.assertIn("modelSelection: unavailable", self.b(root, "brief", "a", "--class", "docs"))

    def test_per_class_routing_precedence_and_sources(self):
        inst = self.routing_instance()
        node = inst.node("root")
        inst.cfg.update(default_runtime="claude", route_runtime_build="codex",
                        route_model_codex="instance-default", route_model_codex_build="instance-build")
        node["fm"].update(runtime="claude", route_runtime_build="codex",
                          route_model_codex="node-default", route_model_codex_build="node-build")
        def route(runtime=None, model=None):
            args = argparse.Namespace(branch=None, job_class="build", runtime=runtime, model=model, effort=None)
            chosen, why = self.m.worker_route(inst, node, args)
            return chosen, args.model, why
        with mock.patch.object(self.m, "usage_config", return_value={"autodetected": True}):
            for remove, runtime, model, source in (
                    (None, "codex", "node-build", "node.md route_model_codex_build"),
                    ("route_model_codex_build", "codex", "node-default", "node.md route_model_codex"),
                    ("route_model_codex", "codex", "instance-build", "instance.md route_model_codex_build")):
                if remove:
                    node["fm"].pop(remove)
                with self.subTest(source=source):
                    chosen, selected, why = route()
                    self.assertEqual((chosen, selected), (runtime, model))
                    self.assertIn(source, why)
            inst.cfg.pop("route_model_codex_build")
            self.assertEqual(route()[:2], ("codex", "instance-default"))
            self.assertIn("instance.md route_model_codex", route()[2])
            self.assertEqual(route("claude", "flag-model")[:2], ("claude", "flag-model"))
            self.assertIn("--model", route("claude", "flag-model")[2])
            self.assertIn("--runtime", route("claude", "flag-model")[2])
            self.assertEqual(route("claude")[:2], ("claude", "runtime default"))
            node["fm"]["route_model_codex_build"] = ""
            self.assertEqual(route()[:2], ("codex", "instance-default"))
            node["fm"].pop("route_runtime_build")
            self.assertEqual(route()[0], "claude")
            self.assertIn("node.md runtime", route()[2])
            node["fm"].pop("runtime")
            self.assertEqual(route()[0], "codex")
            self.assertIn("instance.md route_runtime_build", route()[2])
            inst.cfg.pop("route_runtime_build")
            # Another class keeps class routing enabled, while build falls back.
            inst.cfg["route_runtime_docs"] = "codex"
            self.assertEqual(route()[0], "claude")
            self.assertIn("instance.md default_runtime", route()[2])
            inst.cfg.pop("default_runtime")
            self.assertEqual(route()[0], "codex")
            self.assertIn("default", route()[2])

    def test_inherited_models_preserve_runtime_policy_before_quota(self):
        inst = self.routing_instance()
        inst.cfg["default_runtime"] = "claude"
        root = inst.node("root")
        root_file = self.tmp / "node.md"
        child_file = inst.node_dir("child") / "node.md"
        child_file.parent.mkdir(parents=True)
        # Local class keys retain their old activation of per-class runtime policy.
        cases = [
            ({"route_model_codex_build": "ancestor-class"}, {}, "codex", "ancestor-class"),
            ({"route_model_codex": "ancestor-global"}, {}, "codex", "ancestor-global"),
            ({"route_runtime_build": "claude", "route_model_codex_build": "ancestor-class"}, {}, "codex", "ancestor-class"),
            ({"route_model_codex_build": "ancestor-class"}, {"route_model_codex": "local-global"}, "codex", "local-global"),
            ({"route_model_claude_build": "ancestor-other"}, {"runtime": "claude"}, "claude", "ancestor-other"),
            ({"route_model_codex_build": "ancestor-class"}, {"route_runtime_build": "codex"}, "codex", "ancestor-class"),
            ({"route_model_claude_build": "ancestor-other"}, {"route_model_codex_build": "local-class"}, "claude", "ancestor-other"),
            ({"route_model_claude_build": "ancestor-other"}, {"route_model_claude_build": "local-class"}, "claude", "local-class"),
        ]
        owner = self.m._owners["worker_route"]
        original = owner._m_usage.class_route
        with mock.patch.object(self.m, "usage_config", return_value={"autodetected": True}):
            for ancestor, local, expected_runtime, expected_model in cases:
                with self.subTest(ancestor=ancestor, local=local):
                    self.m.write_fm(root_file, {"id": "root", "owners": "alice", **ancestor}, "")
                    self.m.write_fm(child_file, {"id": "child", **local}, "")
                    args = argparse.Namespace(branch=None, job_class="build", runtime=None, model=None, effort=None)
                    with mock.patch.object(owner._m_usage, "class_route", wraps=original) as route:
                        runtime, _ = self.m.worker_route(inst, inst.node("child"), args)
                    self.assertEqual((runtime, args.model), (expected_runtime, expected_model))
                    self.assertEqual(route.call_args.kwargs["model"], expected_model)
            # CLI choices still win over both local class and inherited model defaults.
            args = argparse.Namespace(branch=None, job_class="build", runtime="codex", model="flag-model", effort=None)
            self.assertEqual(self.m.worker_route(inst, inst.node("child"), args)[0], "codex")
            self.assertEqual(args.model, "flag-model")

    def test_inherited_model_quota_selects_subscription_after_runtime(self):
        inst = self.routing_instance()
        inst.cfg["default_runtime"] = "claude"
        child = inst.node_dir("child")
        child.mkdir(parents=True)
        self.m.write_fm(child / "node.md", {"id": "child"}, "")
        config = self.declare(self.subscription("a"), self.subscription("b"))
        readings = [self.reading(5, weekly=5), self.reading(20, weekly=20)]
        for reading, used in zip(readings, (95, 30)):
            reading["windows"]["seven_day_model:Ancestor"] = {
                "used_percent": used, "resets_at": time.time() + 86400, "models": ["Ancestor"]}
        self.cache(config, readings)
        for key in ("route_model_codex_build", "route_model_codex"):
            with self.subTest(key=key):
                self.m.write_fm(self.tmp / "node.md", {"id": "root", "owners": "alice", key: "Ancestor"}, "")
                args = argparse.Namespace(branch=None, job_class="build", runtime=None, model=None, effort=None)
                runtime, why = self.m.worker_route(inst, inst.node("child"), args)
                selection = json.loads(args.selection_line.split(": ", 1)[1])
                self.assertEqual((runtime, args.model, selection["instanceId"]), ("codex", "Ancestor", "host-b"))
                self.assertNotIn("reached its stop line", why)

    def test_review_inherited_model_reaches_quota_without_runtime_activation(self):
        root = self.repo(self.tmp / "review-inherited")
        inst = self.routing_instance(root)
        child = inst.node_dir("child")
        child.mkdir(parents=True)
        self.m.write_fm(root / "node.md", {"id": "root", "owners": "alice", "route_model_claude_review": "ancestor-review"}, "")
        self.m.write_fm(child / "node.md", {"id": "child"}, "")
        node = inst.node("child")
        self.assertFalse(self.m.class_routing_config(inst, node))
        owner = self.m._owners["cmd_review"]
        original = owner._m_usage.class_route
        args = argparse.Namespace(id="child", branch="main", built_by="codex", by=None,
            job_class=None, model=None, effort=None, base="main", out=None, focus=None,
            contract=None, why_separate=None, allow_unexplained=False)
        with mock.patch.object(owner._m_nodes, "load", return_value=inst), \
                mock.patch.object(self.m, "usage_config", return_value={"autodetected": True}), \
                mock.patch.object(self.m, "review_mechanical", return_value=[]), \
                mock.patch.object(self.m, "job_reviewed"), \
                mock.patch.object(self.m, "record_job_route"), \
                mock.patch.object(owner._m_usage, "class_route", wraps=original) as route, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.m.cmd_review(args)
        self.assertEqual(route.call_args.kwargs["model"], "ancestor-review")
        self.assertEqual(route.call_args.kwargs["runtimes"], ["claude"])
        self.assertIn('"model": "ancestor-review"', output.getvalue())
        self.assertIn("model=root node.md route_model_claude_review", output.getvalue())

    def test_root_node_models_reach_descendants_with_overrides_and_boundary(self):
        inst = self.routing_instance()
        (self.tmp / "node.md").write_text("---\nid: root\nowners: alice\nroute_runtime_build: codex\nroute_model_codex_build: root-build\nroute_model_claude_build: root-other\n---\n")
        for nid in ("child", "child/grandchild", "sealed", "sealed/inner"):
            folder = inst.node_dir(nid)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "node.md").write_text("---\nid: " + nid + "\n" +
                ("owners: team\n" if nid == "sealed" else "") + "---\n")
        inst.cfg.update(route_model_codex_build="instance-build")
        def route(nid, runtime=None, model=None):
            args = argparse.Namespace(branch=None, job_class="build", runtime=runtime, model=model, effort=None)
            chosen, why = self.m.worker_route(inst, inst.node(nid), args)
            return chosen, args.model, why
        def set_node(nid, **values):
            file = inst.node_dir(nid) / "node.md"
            fm, body = self.m.read_fm(file)
            fm.update(values)
            self.m.write_fm(file, fm, body)
        with mock.patch.object(self.m, "usage_config", return_value={"autodetected": True}):
            for nid in ("child", "child/grandchild"):
                self.assertEqual(route(nid)[:2], ("codex", "root-build"))
                self.assertIn("root", route(nid)[2])
            set_node("child", route_model_codex="child-default", runtime="claude")
            self.assertEqual(route("child")[:2], ("claude", "root-other"))
            self.assertEqual(route("child/grandchild")[:2], ("codex", "child-default"))
            set_node("child/grandchild", runtime="claude")
            self.assertEqual(route("child/grandchild")[:2], ("claude", "root-other"))
            self.assertEqual(route("child/grandchild", runtime="codex")[:2], ("codex", "child-default"))
            set_node("child/grandchild", route_model_codex_build="leaf-build", runtime="codex")
            self.assertEqual(route("child/grandchild")[:2], ("codex", "leaf-build"))
            self.assertEqual(route("child/grandchild", model="flag-model")[:2], ("codex", "flag-model"))
            for nid in ("sealed", "sealed/inner"):
                self.assertEqual(route(nid)[:2], ("codex", "instance-build"))
                self.assertNotIn("root-build", route(nid)[2])
            # Runtime-wide legacy model defaults inherit through the same lookup.
            (self.tmp / "node.md").write_text("---\nid: root\nowners: alice\nroute_model_codex: root-legacy\n---\n")
            inst.cfg.pop("route_model_codex_build")
            (inst.node_dir("child") / "node.md").write_text("---\nid: child\n---\n")
            self.assertEqual(route("child")[:2], ("codex", "root-legacy"))

    def test_root_node_model_inheritance_in_cli_briefs(self):
        root = self.init("alice")
        self.b(root, "new", "child", "--title", "Child")
        self.b(root, "new", "child/leaf", "--title", "Leaf")
        self.b(root, "set", "root", "route_model_codex_build=root-model")
        self.b(root, "set", "child", "runtime=")
        self.b(root, "set", "child/leaf", "runtime=")
        cfg, body = self.m.read_fm(root / "instance.md")
        cfg.update(default_runtime="claude", runtimes="claude,codex")
        self.m.write_fm(root / "instance.md", cfg, body)
        children = [root / "nodes/child/node.md", root / "nodes/child/nodes/leaf/node.md"]
        before = [file.read_bytes() for file in children]
        for nid in ("child", "child/leaf"):
            out = self.b(root, "brief", nid, "--runtime", "codex", "--class", "build", "--task", "Fixture work")
            self.assertIn('"model": "root-model"', out)
            self.assertIn("model=root node.md route_model_codex_build", out)
            automatic = self.b(root, "brief", nid, "--class", "build", "--task", "Fixture work")
            self.assertIn("runtime: codex", automatic)
            self.assertIn('"model": "root-model"', automatic)
            self.assertIn("runtime=default; model=root node.md route_model_codex_build", automatic)
        self.assertEqual([file.read_bytes() for file in children], before)
        self.b(root, "set", "child", "route_model_codex=child-model")
        out = self.b(root, "brief", "child/leaf", "--runtime", "codex", "--class", "build", "--task", "Fixture work")
        self.assertIn('"model": "child-model"', out)
        self.assertIn("model=child node.md route_model_codex", out)

    def test_per_class_routing_all_classes_and_unknown_keys(self):
        inst = self.routing_instance()
        node = inst.node("root")
        node["fm"].pop("runtime", None)
        with mock.patch.object(self.m, "usage_config", return_value={"autodetected": True}):
            for kind in self.m.JOB_CLASSES:
                with self.subTest(kind=kind):
                    node["fm"]["route_runtime_" + kind] = "codex"
                    inst.cfg["route_model_codex_" + kind] = "model-" + kind
                    args = argparse.Namespace(branch=None, job_class=kind, runtime=None, model=None, effort=None)
                    runtime, why = self.m.worker_route(inst, node, args)
                    self.assertEqual((runtime, args.model), ("codex", "model-" + kind))
                    self.assertIn("node.md route_runtime_" + kind, why)
                    self.assertIn("instance.md route_model_codex_" + kind, why)
            inst.cfg.pop("route_model_codex_build")
            args = argparse.Namespace(branch=None, job_class="build", runtime=None, model=None, effort=None)
            _, why = self.m.worker_route(inst, node, args)
            self.assertEqual(args.model, "runtime default")
            self.assertIn("model=default", why)
            for cfg in (node["fm"], inst.cfg):
                for key in ("route_runtime_buid", "route_model_codex_revew"):
                    with self.subTest(key=key), contextlib.redirect_stderr(io.StringIO()) as err:
                        cfg[key] = "codex"
                        args = argparse.Namespace(branch=None, job_class="build", runtime="codex", model="explicit", effort=None)
                        with self.assertRaises(SystemExit) as exc:
                            self.m.worker_route(inst, node, args)
                        self.assertIn(key, str(exc.exception))
                        self.assertIn("class", str(exc.exception))
                        cfg.pop(key)

    def routing_instance(self, root=None):
        root = root or self.tmp
        (root / "instance.md").write_text("---\nname: alice\nruntimes: claude,codex\nhost: t3\n---\n")
        (root / "node.md").write_text("---\nid: root\ntitle: Example\n---\n")
        return self.m.Instance(root)

    def test_class_model_quota_before_subscription_selection(self):
        # No initialized brain or Git repository is needed for cached routing.
        inst = self.routing_instance()
        inst.cfg.update(name="alice", route_runtime_review="claude", route_model_claude_review="Fable")
        inst.runtimes = ["claude", "codex"]
        node = inst.node("root")
        config = self.declare(self.subscription("a", runtime="claude"), self.subscription("b", runtime="claude"))
        readings = [self.reading(5, weekly=5), self.reading(20, weekly=20)]
        for reading, used in zip(readings, (95, 30)):
            reading["windows"]["seven_day_model:Fable"] = {
                "used_percent": used, "resets_at": time.time() + 86400, "models": ["Fable"]}
        self.cache(config, readings)
        def route(model=None):
            a = argparse.Namespace(branch=None, job_class="review", runtime=None, model=model, effort=None)
            _, why = self.m.worker_route(inst, node, a)
            return json.loads(a.selection_line.split(": ", 1)[1]), why
        selection, why = route()
        self.assertEqual(selection["instanceId"], "host-b")
        self.assertEqual(selection["model"], "Fable")
        self.assertIn("model=instance.md route_model_claude_review", why)
        self.assertNotIn("reached its stop line", why)
        self.assertEqual(route("Fable")[0], selection)
        readings[1]["windows"]["seven_day_model:Fable"]["used_percent"] = 95
        self.cache(config, readings)
        self.assertIn("reached its stop line", route()[1])

    def test_review_class_model_source_label(self):
        root = self.repo(self.tmp / "review")
        inst = self.routing_instance(root)
        inst.runtimes = ["claude", "codex"]
        inst.cfg["route_model_claude_review"] = "instance-review"
        node = inst.node("root")
        owner = self.m._owners["cmd_review"]
        def render(model=None):
            a = argparse.Namespace(id="root", branch="main", built_by="codex", by=None,
                job_class=None, model=model, effort=None, base="main", out=None, focus=None,
                contract=None, why_separate=None, allow_unexplained=False)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.m.cmd_review(a)
            return out.getvalue()
        with mock.patch.object(owner._m_nodes, "load", return_value=inst), \
                mock.patch.object(inst, "node", return_value=node), \
                mock.patch.object(self.m, "review_mechanical", return_value=[]), \
                mock.patch.object(self.m, "job_reviewed"), \
                mock.patch.object(self.m, "record_job_route"):
            for cfg, model, source in ((inst.cfg, "instance-review", "instance.md"),
                                       (node["fm"], "node-review", "node.md")):
                cfg["route_model_claude_review"] = model
                text = render()
                self.assertIn('"model": "' + model + '"', text)
                self.assertIn("model=" + source + " route_model_claude_review", text)
            self.assertIn("model=--model", render("explicit-review"))
            node["fm"].clear()
            inst.cfg.clear()
            self.assertNotIn("Routing sources:", render())

    def test_compound_unknown_class_keys_rejected(self):
        inst = self.routing_instance()
        inst.runtimes = ["claude", "codex", "custom_engine"]
        node = inst.node("root")
        for cfg in (inst.cfg, node["fm"]):
            for key in ("route_model_codex_code_review", "route_model_custom_engine_code_review"):
                cfg[key] = "typo-model"
                with self.subTest(key=key), self.assertRaises(SystemExit) as exc:
                    self.m.class_routing_config(inst, node)
                self.assertIn(key, str(exc.exception))
                cfg.pop(key)
            cfg["route_model_custom_engine"] = "runtime-default"
            self.assertFalse(self.m.class_routing_config(inst, node))
            cfg["route_model_custom_engine_review"] = "class-review"
            self.assertTrue(self.m.class_routing_config(inst, node))
            cfg.clear()

    def test_per_class_brief_cli_and_legacy_bytes(self):
        root = self.init("alice")
        self.b(root, "new", "a", "--title", "A")
        self.b(root, "set", "a", "route_model_codex=legacy-model")
        # Render the entire legacy brief using the pre-change resolver as an oracle.
        owner = self.m._owners["cmd_brief"]
        args = argparse.Namespace(id="a", job_class="build", task="x", branch=None, runtime=None, model=None,
            effort=None, import_study=False, small=False, task_file=None, context_file=None, why_separate=None,
            allow_unexplained=False, to=None, peer=None, out=None)
        def legacy_route(inst, node, a, extra=()):
            self.assertEqual(list(extra), [])
            row = self.m.open_job(self.m.read_jobs(node) or [], a.branch)
            kind = self.m.job_class(node, row, a.job_class)
            runtime, sub, usage = self.m.class_route(inst, kind, runtime=a.runtime or node["fm"].get("runtime"),
                model=a.model, weekly_cap=node["fm"].get("route_weekly_cap", inst.cfg.get("route_weekly_cap", 60)))
            a.job_class = kind
            a.model = a.model or node["fm"].get("route_model_" + runtime) or inst.cfg.get("route_model_" + runtime) or "runtime default"
            a.effort = self.m.job_effort(node, row, kind, a.effort)
            if a.model == "runtime default":
                a.selection_line = f"modelSelection: unavailable; set route_model_{runtime} or --model (reasoningEffort: {a.effort})."
            else:
                a.selection_line = "modelSelection: " + json.dumps({"instanceId": (sub or {}).get("host_provider_instance_id") or runtime,
                    "model": a.model, "options": {"reasoningEffort": a.effort}})
            return runtime, usage
        with mock.patch.dict(os.environ, {"BRAIN_ROOT": str(root)}):
            with mock.patch.object(owner._m_knowledge, "worker_route", side_effect=legacy_route), contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                self.m.cmd_brief(args)
            expected = out.getvalue() + err.getvalue()
        self.assertEqual(self.b(root, "brief", "a", "--class", "build", "--task", "x"), expected)
        self.assertNotIn("Routing sources:", expected)
        self.b(root, "set", "a", "route_runtime_build=codex", "route_model_codex_build=class-model")
        text = self.b(root, "brief", "a", "--class", "build", "--task", "x")
        self.assertIn('"model": "class-model"', text)
        self.assertIn("model=node.md route_model_codex_build", text)
        self.assertIn("runtime=node.md route_runtime_build", text)
        self.b(root, "set", "a", "runtime=", "route_runtime_build=", "route_model_codex_build=", "route_model_codex=")
        file = root / "instance.md"
        file.write_text(file.read_text().replace("---\n", "---\nroute_runtime_build: codex\nroute_model_codex_build: instance-class-model\n", 1))
        text = self.b(root, "brief", "a", "--class", "build", "--task", "x")
        self.assertIn('"model": "instance-class-model"', text)
        self.assertIn("model=instance.md route_model_codex_build", text)
        self.assertIn("runtime=instance.md route_runtime_build", text)
        self.b(root, "set", "a", "route_runtime_unknown=codex")
        self.assertIn("unknown routing class", self.b(root, "brief", "a", ok=False))

    def test_class_route_autodetection_never_refreshes_providers(self):
        root = self.init("alice")
        inst = self.m.Instance(root)
        with mock.patch.object(self.m, "usage_refresh", side_effect=AssertionError("provider called")) as refresh:
            for kind, expected in (("build", "codex"), ("review", "claude")):
                routed, sub, why = self.m.class_route(inst, kind)
                self.assertEqual(routed, expected)
                self.assertIsNone(sub)
                self.assertIn("Usage unknown", why)
            refresh.assert_not_called()

    def test_class_route_single_runtime_and_autodetection_stays_offline(self):
        root = self.init("alice")
        inst = self.m.Instance(root)
        self.cache(self.declare(), [])
        with mock.patch.object(self.m, "usage_refresh", side_effect=AssertionError("provider called")):
            for runtimes, kind, expected in ((["claude"], "build", "claude"),
                                              (["codex"], "review", "codex")):
                inst.runtimes = runtimes
                routed, sub, why = self.m.class_route(inst, kind)
                self.assertEqual(routed, expected)
                self.assertIsNone(sub)
                self.assertIn("default unavailable", why)
                self.assertIn("Usage unknown", why)
            inst.runtimes = ["claude", "codex"]
            self.assertEqual(self.m.class_route(inst, "build")[0], "codex")
            self.assertEqual(self.m.class_route(inst, "review")[0], "claude")
        self.b(root, "set", "root", "runtime=")
        file = root / "instance.md"
        file.write_text(file.read_text().replace("runtimes: claude,codex", "runtimes: claude"))
        # CLI review refuses a same-runtime reviewer on a one-runtime instance.
        self.git("branch", "job", cwd=root)
        out = self.b(root, "review", "root", "--branch", "job", "--built-by", "claude", ok=False)
        self.assertIn("nothing to cross-review", out)

    def test_class_route_never_switches_to_exhausted_alternative(self):
        root = self.init("alice")
        inst = self.m.Instance(root)
        config = self.declare(self.subscription("cx"), self.subscription("cl", runtime="claude"))
        for five_hour, weekly in ((10, 97), (99, 35), (10, 70)):
            self.cache(config, [self.reading(10, weekly=65), self.reading(five_hour, weekly=weekly)])
            self.assertEqual(self.m.class_route(inst, "build", cached_only=True)[0], "codex")
        self.cache(config, [self.reading(10, weekly=90), self.reading(10, weekly=70)])
        self.assertEqual(self.m.class_route(inst, "build", cached_only=True)[0], "codex")

    def test_pinned_alternative_requires_all_windows_below_stop(self):
        root = self.init("alice")
        inst = self.m.Instance(root)
        config = self.declare(self.subscription("cx"), self.subscription("cl", runtime="claude"))
        self.cache(config, [self.reading(99, weekly=99), self.reading(10, weekly=99)])
        why = self.m.class_route(inst, "build", runtime="codex", cached_only=True)[2]
        self.assertNotIn("Alternative:", why)

    def test_later_contract_class_overrides_default_and_reports_source(self):
        root = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(root, "new", "a", "--title", "A", "--home", str(home))
        self.b(root, "set", "a", "runtime=")
        self.b(root, "wt", "add", "a", "job")
        self.b(root, "brief", "a", "--branch", "job")
        node = self.m.Instance(root).node("a")
        row = self.m.open_job(self.m.read_jobs(node), "job")
        contract = self.m.contract_file(node, row)
        contract.parent.mkdir(exist_ok=True)
        contract.write_text("## Class\ndiagnose\n")
        text = self.b(root, "brief", "a", "--branch", "job")
        self.assertIn("class: diagnose", text)
        self.assertIn("runtime: claude", text)
        contract.write_text("## Class\n`docs` because this job documents the interface\n")
        self.assertEqual(self.m.job_class(node, row), "docs")
        contract.write_text("## Class\nunknown\n")
        out = self.b(root, "brief", "a", "--branch", "job", ok=False)
        self.assertIn(contract.name, out)
        self.assertIn("## Class: job class", out)
        contract.unlink()
        self.assertEqual(self.m.job_class(node, {**row, "Class": "future-class"}), "build")

    def test_class_section_first_word_and_unknown_ledger_class(self):
        root = self.init("alice")
        node = self.m.Instance(root).node("root")
        row = {"Job": "job", "Started": "2026-01-01", "Class": "future-class"}
        self.assertEqual(self.m.job_class(node, row), "build")
        path = self.m.contract_file(node, row)
        path.parent.mkdir(exist_ok=True)
        path.write_text("## Class\n`docs` with a short explanation\n")
        self.assertEqual(self.m.job_class(node, row), "docs")
        path.write_text("## Class\ninvalid explanation\n")
        with self.assertRaises(SystemExit) as stopped:
            self.m.job_class(node, row)
        self.assertIn(path.name, str(stopped.exception))

    def test_review_columns_survive_v1_v2_roundtrip(self):
        root = self.init("alice")
        inst = self.m.Instance(root)
        node = inst.node("root")
        ledger = node["dir"] / "job-ledger.md"
        for version in (1, 2):
            ledger.write_text(f"<!-- brain:job-ledger v{version} -->\n"
                "| Job | Started | Review class | Review runtime | Review model | Future |\n"
                "| --- | --- | --- | --- | --- | --- |\n"
                "| job | 2026-01-01T00:00:00+00:00 | review | claude | sample-model | keep |\n")
            rows = self.m.read_jobs(node)
            self.m.write_jobs(inst, node, rows)
            reread = self.m.read_jobs(node)
            for key in ("Review class", "Review runtime", "Review model", "Future"):
                self.assertEqual(reread[0][key], rows[0][key])

    def test_specialist_defaults_and_implicit_review_are_not_pins(self):
        root = self.init("alice")
        self.b(root, "set", "root", "runtime=")
        self.b(root, "specialist", "new", "qa", "--title", "QA", "--runtime", "claude")
        config = self.declare(self.subscription("cx"), self.subscription("cl", runtime="claude"))
        self.cache(config, [self.reading(10, weekly=10), self.reading(10, weekly=20)])
        text = self.b(root, "hire", "qa", "root", "--class", "build", "--task", "x")
        self.assertIn("runtime: codex", text)
        self.assertNotIn("Runtime pin:", text)

    def test_implicit_review_is_not_a_runtime_pin(self):
        root = self.init("alice")
        self.b(root, "set", "root", "runtime=")
        self.git("branch", "job", cwd=root)
        text = self.b(root, "review", "root", "--branch", "job", "--built-by", "codex")
        self.assertIn("Reviewer runtime: claude", text)
        self.assertNotIn("Runtime pin:", text)
        text = self.b(root, "review", "root", "--branch", "job", "--built-by", "codex", "--by", "claude")
        self.assertIn("Runtime pin: claude", text)

    def test_class_router_weekly_policy_and_pins(self):
        root = self.init("alice")
        inst = self.m.Instance(root)
        config = self.declare(self.subscription("cx"), self.subscription("cl", runtime="claude"))
        self.cache(config, [self.reading(10, weekly=65), self.reading(20, weekly=35)])
        with mock.patch.object(self.m, "usage_refresh", side_effect=AssertionError("provider called")):
            routed, sub, why = self.m.class_route(inst, "build", cached_only=True)
            self.assertEqual(routed, "codex")
            self.assertEqual(sub["id"], "cx")
            self.assertIn("Weekly meter 65%; keeping codex", why)
            self.assertEqual(self.m.class_route(inst, "review", cached_only=True)[0], "claude")
            self.assertEqual(self.m.class_route(inst, "diagnose", cached_only=True)[0], "claude")
            self.assertEqual(self.m.class_route(inst, "build", runtime="codex", cached_only=True)[0], "codex")
            self.assertEqual(self.m.class_route(inst, "build", weekly_cap=70, cached_only=True)[0], "codex")
        self.cache(config, [self.reading(10, weekly=10), self.reading(90, weekly=95)])
        for kind in ("review", "diagnose"):
            self.assertEqual(self.m.class_route(inst, kind, cached_only=True)[0], "claude")
        for kind in ("build", "ui", "migration", "docs"):
            self.assertEqual(self.m.class_route(inst, kind, cached_only=True)[0], "codex")
        self.assertFalse(self.m.class_allows_fallback(None))
        for kind in ("diagnose", "review"):
            self.assertFalse(self.m.class_allows_fallback(kind))
        self.cache(config, [self.reading(5, weekly=60), self.reading(1, weekly=10)])
        self.assertEqual(self.m.class_route(inst, "build", cached_only=True)[0], "codex")
        self.cache(config, [self.reading(5, weekly=65), self.reading(source="unknown")])
        self.assertEqual(self.m.class_route(inst, "build", cached_only=True)[0], "codex")
        self.cache(config, [self.reading(5, weekly=65, age=1000), self.reading(5, weekly=10)])
        self.assertEqual(self.m.class_route(inst, "build", cached_only=True)[0], "codex")

    def test_class_brief_records_route_contract_and_model(self):
        root = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(root, "new", "a", "--title", "A", "--home", str(home))
        self.b(root, "set", "a", "runtime=", "route_model_claude=example-claude", "route_model_codex=example-codex")
        self.b(root, "wt", "add", "a", "job")
        inst = self.m.Instance(root)
        node = inst.node("a")
        row = self.m.open_job(self.m.read_jobs(node), "job")
        contract = self.m.contract_file(node, row)
        contract.parent.mkdir(exist_ok=True)
        contract.write_text("## Class\nbuild\n")
        config = self.declare(self.subscription("cx"), self.subscription("cl", runtime="claude"))
        self.cache(config, [self.reading(10, weekly=65), self.reading(20, weekly=35)])
        text = self.b(root, "brief", "a", "--branch", "job", "--task", "review diagnose prose cannot classify")
        for part in ("class: build", "runtime: codex", "model: example-codex", "Weekly meter 65%; keeping codex"):
            self.assertIn(part, text)
        row = self.m.open_job(self.m.read_jobs(node), "job")
        self.assertEqual((row["Class"], row["Runtime"], row["Model"]), ("build", "codex", "example-codex"))
        self.assertEqual(self.m.job_class(node, row), "build")
        text = self.b(root, "brief", "a", "--branch", "job", "--class", "review", "--runtime", "codex", "--model", "example-codex")
        self.assertIn("runtime: codex", text)
        self.assertEqual(self.m.open_job(self.m.read_jobs(node), "job")["Class"], "review")
        self.b(root, "set", "a", "runtime=codex")
        self.assertIn("runtime: codex", self.b(root, "brief", "a", "--branch", "job", "--class", "review"))
        self.b(root, "brief", "a", "--class", "unknown", ok=False)
        rows = self.m.read_jobs(node)
        rows[0]["Future column"] = "kept"
        self.m.write_jobs(inst, node, rows)
        self.assertEqual(self.m.read_jobs(node)[0]["Future column"], "kept")
        self.b(root, "specialist", "new", "qa", "--title", "QA")
        hired = self.b(root, "hire", "qa", "a", "--branch", "job", "--class", "docs", "--runtime", "claude", "--model", "hire-model", "--task", "x")
        self.assertIn("class: docs", hired)
        self.assertEqual(self.m.open_job(self.m.read_jobs(node), "job")["Model"], "hire-model")
        reviewed = self.b(root, "review", "a", "--branch", "job", "--class", "review", "--built-by", "codex", "--runtime", "claude")
        self.assertIn("Reviewer runtime: claude", reviewed)
        self.assertIn("class: review", reviewed)
        # A reviewer never replaces the worker's route metadata.
        final = self.m.open_job(self.m.read_jobs(node), "job")
        self.assertEqual(final["Class"], "docs")
        self.assertEqual(final["Review class"], "review")
        self.assertEqual(final["Review runtime"], "claude")
        self.assertEqual(final["Review model"], "example-claude")

    def test_subscription_stop_percent_defaults_validation_and_routing(self):
        root = self.init("alice")
        inst = self.m.Instance(root)
        one = {**self.subscription(), "stop_percent": 50}
        two = self.subscription("two")
        config = self.declare(one, two, stop_percent=80)
        self.assertEqual([s["stop_percent"] for s in config["subscriptions"]], [50, 80])
        self.cache(config, [self.reading(50, weekly=50), self.reading(60, weekly=60)])
        selected, _ = self.m.usage_route(inst, ["codex"], cached_only=True)
        self.assertEqual(selected["id"], "two")
        selected = self.m.class_route(inst, "build", cached_only=True)[1]
        self.assertEqual(selected["id"], "two")
        self.declare({**one, "stop_percent": 95}, two, stop_percent=80)
        with mock.patch.object(self.m, "usage_refresh", side_effect=AssertionError("policy change refreshed readings")):
            rows, _ = self.m.usage_rows(inst, before_launch=True)
        self.assertEqual(rows[0]["windows"]["seven_day"]["used_percent"], 50)
        self.assertEqual(rows[0]["stop_percent"], 95)
        self.assertEqual(self.m.usage_route(inst, ["codex"], cached_only=True)[0]["id"], "one")
        output = self.b(root, "usage", "--json")
        self.assertEqual([s["stop_percent"] for s in json.loads(output)["subscriptions"]], [95, 80])
        for value in (0, -1, 101, True, "70", None, float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.declare({**one, "stop_percent": value})

    def test_usage_defaults_and_config_isolation(self):
        with mock.patch.object(self.m.shutil, "which", return_value="installed"):
            config = self.m.usage_config()
        self.assertEqual([s["brains"] for s in config["subscriptions"]], [["*"], ["*"]])
        self.assertEqual(config["subscriptions"][1]["config_dir"], str(self.tmp / ".codex"))
        self.assertEqual(self.m.usage_file(), self.cfg / "usage.json")
        self.env["CODEX_HOME"] = str(self.tmp / "alternate")
        with mock.patch.dict(os.environ, self.env), mock.patch.object(self.m.shutil, "which", return_value="installed"):
            self.assertEqual(self.m.usage_config()["subscriptions"][1]["config_dir"], self.env["CODEX_HOME"])
        sub = self.subscription(brains=[])
        self.assertEqual(self.declare(sub)["subscriptions"][0]["brains"], [])
        self.declare(sub, stop_percent=70)
        self.assertEqual(self.m.usage_config()["stop_percent"], 70)
        with self.assertRaises(ValueError): self.declare(sub, stop_percent=float("nan"))
        (self.cfg / "subscriptions.json").write_text("malformed")
        with self.assertRaises(ValueError): self.m.usage_config()

    def test_usage_real_codex_protocol_and_account_deduplication(self):
        self.codex_stub()
        one, two = self.subscription(), self.subscription("two", brains=["work"])
        alias = {**one, "id": "alias", "host_provider_instance_id": "host-alias", "brains": ["other"]}
        self.fixture(one); self.fixture(two)
        config = self.declare(one, two, alias)
        data = self.m.usage_refresh(config)
        self.assertEqual(len(data["accounts"]), 1)
        self.assertEqual(len(data["subscriptions"]), 3)
        r = next(iter(data["accounts"].values()))
        self.assertEqual(r["source"], "lookup")
        self.assertEqual(list(r["windows"]), ["seven_day", "five_hour"])
        self.assertTrue(r["windows"]["five_hour"]["absent"])
        requests = (Path(one["config_dir"]) / "requests.txt").read_text().splitlines()
        self.assertEqual(requests, ["initialize", "initialized", "account/read", "account/rateLimits/read"])
        rows, _ = self.m.usage_rows()
        self.assertEqual(rows[0]["account_key"], rows[1]["account_key"])
        self.assertEqual(rows[2]["brains"], ["other"])
        self.assertNotIn("same-account", self.m.usage_file().read_text())
        self.assertNotIn("credits", self.m.usage_file().read_text())

    def test_usage_absent_five_hour_is_not_a_missing_reading(self):
        root = self.init("alice")
        self.codex_stub()
        one, missing = self.subscription(), self.subscription("missing")
        data = self.fixture(one)
        config = self.declare(one, missing)
        self.m.usage_refresh(config)
        text = self.b(root, "usage")
        self.assertRegex(text, r"codex\s+one\s+85%\s+none\s+24%")
        self.assertIn("missing: no reading yet: run `brain usage --refresh`", text)
        selected, reason = self.m.usage_route(self.m.Instance(root), ["codex"])
        self.assertEqual(selected["id"], "one")
        self.assertNotIn("unknown", reason)
        for rates in ({}, {"primary": data["rateLimits"]["primary"]},
                      {"primary": data["rateLimits"]["primary"], "secondary": {"usedPercent": "bad"}}):
            self.assertNotIn("five_hour", self.m.usage_windows(rates, "codex", absences=True))
        # The declaration survives a failed lookup, but newer actual readings replace it.
        absent = self.m.usage_cache()["accounts"][selected["account_key"]]
        later = self.reading(90)
        merged = self.m.usage_merge(absent, later)
        self.assertEqual(merged["windows"]["five_hour"]["used_percent"], 90)
        self.assertNotIn("absent", merged["windows"]["five_hour"])
        self.assertEqual(self.m.usage_merge(absent, self.reading(source="unknown")), absent)
        # A successful plan change can remove a previously observed five-hour limit.
        removed = self.m.usage_merge(self.reading(90, age=60), absent)
        self.assertTrue(removed["windows"]["five_hour"]["absent"])
        self.cache(config, [removed, self.reading(source="unknown")])
        self.assertIn("none", self.b(root, "usage"))
        self.assertNotIn("Stop starting", self.m.usage_route(self.m.Instance(root), ["codex"])[1])

    def test_usage_resume_switch_uses_replacement_subscription_and_local_fallback(self):
        root = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(root, "new", "a", "--title", "A", "--home", str(home), "--runtime", "claude")
        self.b(root, "wt", "add", "a", "job")
        wt = self.tmp / "app-work/worktrees/job"
        self.git("commit", "--allow-empty", "-qm", "partial", cwd=wt)
        sha = self.git("rev-parse", "HEAD", cwd=wt).strip()
        config = self.declare(self.subscription("old", runtime="claude"),
                              self.subscription("one"), self.subscription("two"))
        self.cache(config, [self.reading(90), self.reading(25), self.reading(25)])
        self.b(root, "set", "root", "fallback=host-old,host-two,host-one")
        self.assertEqual(self.m.Instance(root).local["fallback"], ["host-old", "host-two", "host-one"])
        before = self.b(root, "brief", "a", "--branch", "job")
        self.assertIn("Pinned runtime claude", before)
        self.b(root, "jobs", "a", "--branch", "job", "--switch", "host-old -> host-two: usage limit")
        brief = self.b(root, "brief", "a", "--branch", "job", "--runtime", "codex", "--task", "Finish it")
        self.assertIn("runtime: codex", brief)
        self.assertIn("Subscription: two (two) · host instance: host-two", brief)
        self.assertNotIn("Subscription: old", brief)
        self.assertNotIn("host instance: host-one", brief)
        self.assertNotIn("Stop starting", brief)
        self.assertNotIn("Pinned runtime claude", brief)
        self.assertIn("## Resume existing work", brief)
        self.assertIn(sha, brief)
        self.assertIn("Continue from these commits", brief)
        self.assertIn("host-old -> host-two: usage limit", brief)
        self.assertIn("brain handback a --branch job", brief)
        self.assertIn("--lessons", brief)
        self.assertNotIn("--record", brief)
        self.assertIn("Finish it", brief)
        # Headroom still takes priority over the configured fallback order.
        self.cache(config, [self.reading(90), self.reading(10), self.reading(25)])
        brief = self.b(root, "brief", "a", "--branch", "job", "--runtime", "codex")
        self.assertIn("host instance: host-one", brief)

    def test_usage_codex_old_account_response_uses_account_id_only(self):
        self.codex_stub()
        sub = self.subscription()
        self.fixture(sub)
        data = json.loads((Path(sub["config_dir"]) / "fixture.json").read_text())
        data["account"] = {"type": "chatgpt", "email": "private@example.test"}
        (Path(sub["config_dir"]) / "fixture.json").write_text(json.dumps(data))
        (Path(sub["config_dir"]) / "auth.json").write_text(json.dumps({"tokens": {
            "account_id": "account-id", "access_token": "NEVER-CACHE-THIS"}}))
        out = self.m.usage_refresh(self.declare(sub))
        self.assertIn(self.m.usage_key("codex", "account-id"), out["accounts"])
        self.assertNotIn("NEVER-CACHE-THIS", json.dumps(out))
        self.assertNotIn("private@example.test", json.dumps(out))

    def test_usage_claude_endpoint_credentials_and_failure_degrade(self):
        platform = mock.patch.object(self.m.sys, "platform", "linux")
        platform.start(); self.addCleanup(platform.stop)
        import http.server
        requests, status = [], [200]
        payload = {"five_hour": {"utilization": 35, "resets_at": "2026-10-20T00:00:00Z"},
                   "seven_day": {"utilization": 44, "resets_at": "2026-10-25T00:00:00Z"},
                   "seven_day_sonnet": {"utilization": 99}, "account_id": "claude-account"}
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization"), self.headers.get("anthropic-beta")))
                self.send_response(status[0])
                if status[0] == 302:
                    self.send_header("Location", "/stolen")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            sub = self.subscription(runtime="claude")
            credential = Path(sub["config_dir"]) / ".credentials.json"
            credential.write_text(json.dumps({"claudeAiOauth": {"accessToken": "TEST-SECRET"}}))
            with mock.patch.dict(os.environ, {"BRAIN_USAGE_TEST_URL": f"http://127.0.0.1:{server.server_port}/usage"}):
                data = self.m.usage_refresh(self.declare(sub))
                self.assertEqual(requests[0], ("/usage", "Bearer TEST-SECRET", "oauth-2025-04-20"))
                self.assertEqual(next(iter(data["accounts"].values()))["windows"]["five_hour"]["used_percent"], 35)
                self.assertNotIn("TEST-SECRET", self.m.usage_file().read_text())
                before = len(requests)
                credential.write_text(json.dumps({"claudeAiOauth": {"accessToken": "OLD", "expiresAt": 1000}}))
                self.assertIn("login expired", self.m.usage_reading(sub)[1]["note"])
                self.assertEqual(len(requests), before)  # an expired token is never sent
                credential.unlink()
                with mock.patch.dict(os.environ, {"BRAIN_USAGE_TEST_TOKEN": "INJECTED"}):
                    self.assertEqual(self.m.usage_claude(sub)[0]["seven_day"]["used_percent"], 44)
                payload.clear(); payload["five_hour"] = {"utilization": "bad"}
                with mock.patch.dict(os.environ, {"BRAIN_USAGE_TEST_TOKEN": "INJECTED"}):
                    data = self.m.usage_refresh(self.m.usage_config())
                    self.assertEqual(next(iter(data["accounts"].values()))["source"], "lookup")
                    status[0] = 503
                    _, reading = self.m.usage_reading(sub)
                    self.assertEqual(reading["source"], "unknown")
                    self.assertIn("HTTP 503", reading["note"])
                    status[0] = 401
                    self.assertIn("login expired", self.m.usage_reading(sub)[1]["note"])
                    status[0] = 200
                    payload.update({"five_hour": {"utilization": 5}, "seven_day": {"utilization": 6}})
                    self.m.usage_refresh(self.m.usage_config())
                    status[0] = 401
                    kept = next(iter(self.m.usage_refresh(self.m.usage_config())["accounts"].values()))
                    self.assertEqual(kept["windows"]["seven_day"]["used_percent"], 6)
                    self.assertIn("login expired", kept["note"])
                    status[0] = 302
                    before = len(requests)
                    self.assertEqual(self.m.usage_reading(sub)[1]["source"], "unknown")
                    self.assertEqual(len(requests), before + 1)
                    self.assertNotIn("/stolen", [r[0] for r in requests])
            with mock.patch.dict(os.environ, {"BRAIN_USAGE_TEST_URL": "http://external.invalid/usage"}):
                with self.assertRaises(ValueError): self.m.usage_claude(sub)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_usage_keychain_security_pipe_cooldown_and_cycle(self):
        env = {"BRAIN_USAGE_TEST_NO_KEYCHAIN": "0", "USER": "test-user"}
        cfg = str(self.tmp / ".claude")
        with mock.patch.dict(os.environ, env), mock.patch.object(self.m.sys, "platform", "darwin"), \
                mock.patch.object(self.m.time, "monotonic", return_value=100) as clock, \
                mock.patch.object(self.m.subprocess, "run", side_effect=subprocess.TimeoutExpired("keychain", 2)) as run:
            self.assertEqual(self.m.usage_keychain(cfg)[0], {})
            self.m._KEYCHAIN_CYCLE_BLOCKED.clear()  # next refresh, still in cooldown
            self.assertIn("cooldown", self.m.usage_keychain(cfg)[1])
            self.assertEqual(run.call_count, 2)  # the default folder has two possible names
            self.assertEqual(run.call_args_list[0].args[0], ["/usr/bin/security", "find-generic-password", "-s",
                             "Claude Code-credentials", "-a", "test-user", "-w"])
            self.assertTrue(run.call_args.args[0][3].startswith("Claude Code-credentials-"))
            self.assertEqual(run.call_args.kwargs["timeout"], 2)
            self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(run.call_args.kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
            clock.return_value = 701
            run.side_effect = None
            run.return_value = subprocess.CompletedProcess([], 0, b'{"claudeAiOauth":{"accessToken":"TEST"}}')
            self.assertEqual(self.m.usage_keychain(cfg)[0]["claudeAiOauth"]["accessToken"], "TEST")
            self.assertEqual(run.call_count, 4)
            self.assertNotIn("TEST", str(run.call_args))
            run.reset_mock()
            self.m.usage_keychain(str(self.tmp / "other"))
            self.assertEqual(run.call_count, 1)  # another folder has only its suffixed name
            self.assertTrue(run.call_args.args[0][3].startswith("Claude Code-credentials-"))
            # A host that sets CLAUDE_CONFIG_DIR=~/.claude renews the suffixed login; the plain one goes stale.
            def login(args, **kwargs):
                old = args[3] == "Claude Code-credentials"
                return subprocess.CompletedProcess([], 0, json.dumps({"claudeAiOauth": {
                    "accessToken": "OLD" if old else "NEW", "expiresAt": 1000 if old else 9e12}}).encode())
            run.side_effect = login
            self.m._KEYCHAIN_CYCLE_BLOCKED.clear()
            self.assertEqual(self.m.usage_keychain(cfg)[0]["claudeAiOauth"]["accessToken"], "NEW")
            self.assertEqual(self.m.usage_keychain(cfg)[1], "")
            # An entry without a usable login, or without an expiry, never beats a dated login.
            for stale in ({"other": 1}, {"claudeAiOauth": {"accessToken": "UNDATED"}}):
                run.side_effect = lambda args, stale=stale, **kw: subprocess.CompletedProcess([], 0, json.dumps(
                    stale if args[3] == "Claude Code-credentials" else
                    {"claudeAiOauth": {"accessToken": "NEW", "expiresAt": 9e12}}).encode())
                self.m._KEYCHAIN_CYCLE_BLOCKED.clear()
                self.assertEqual(self.m.usage_keychain(cfg)[0]["claudeAiOauth"]["accessToken"], "NEW")

    def test_usage_claude_limits_and_fractional_timestamps(self):
        for suffix in (".57+00:00", ".576595+00:00", ".5Z", ".5765Z", ".576595123Z", "Z"):
            stamp = self.m.usage_stamp("2026-10-19T17:39:59" + suffix)
            self.assertIsNotNone(stamp, suffix)
        # Assert normalization even on interpreters that also accept arbitrary fractions.
        real = datetime.datetime
        class StrictDateTime(real):
            @classmethod
            def fromisoformat(cls, value):
                import re
                fraction = re.search(r"\.(\d+)", value)
                if fraction and len(fraction[1]) not in (3, 6):
                    raise ValueError("Python 3.9 fraction")
                return real.fromisoformat(value)
        with mock.patch.object(self.m.datetime, "datetime", StrictDateTime):
            self.assertIsNotNone(self.m.usage_stamp("2026-10-19T17:39:59.57+00:00"))
        windows = self.m.usage_windows({"five_hour": {"utilization": 99}, "limits": [None,
            {"kind": "session", "percent": 25, "is_active": False, "resets_at": "2026-10-19T17:39:59.57Z"},
            {"kind": "weekly_all", "percent": 86},
            {"kind": "weekly_scoped", "percent": 0, "scope": {"model": {"id": None, "display_name": "Fable"}}},
            {"kind": "unknown", "percent": 100}]}, "claude")
        self.assertEqual(windows["five_hour"]["used_percent"], 25)
        self.assertEqual(windows["seven_day"]["used_percent"], 86)
        self.assertEqual(windows["seven_day_model:Fable"]["models"], ["Fable"])
        self.assertEqual(len(windows), 3)
        for key in ("limitId", "limit_id"):
            self.assertIn("five_hour", self.m.usage_windows({key: None,
                          "primary": {"windowDurationMins": 300, "usedPercent": 20}}, "codex"))
        self.assertEqual(self.m.usage_windows({"limitId": None, "limit_id": "other",
                         "primary": {"windowDurationMins": 300, "usedPercent": 20}}, "codex"), {})

    def test_usage_logs_equal_stamps_and_old_files(self):
        sub = self.subscription(runtime="claude")
        folder = Path(sub["config_dir"]) / "projects/project"
        folder.mkdir(parents=True)
        for i, name in enumerate(("five_hour", "seven_day")):
            path = folder / (str(i) + ".jsonl")
            path.write_text(json.dumps({"timestamp": "2026-10-19T00:00:00Z", "apiError": "usage_limit_reached",
                "apiErrorParams": {"rate_limit_info": {"status": "rejected", "rateLimitType": name}}}))
        self.assertEqual(set(self.m.usage_logs(sub)[0]), {"five_hour", "seven_day"})
        old = time.time() - 9 * 86400
        os.utime(path, (old, old))
        real_open = Path.open
        def checked_open(p, *args, **kwargs):
            self.assertNotEqual(p, path, "old transcript was opened")
            return real_open(p, *args, **kwargs)
        with mock.patch.object(Path, "open", checked_open):
            self.assertEqual(set(self.m.usage_logs(sub)[0]), {"five_hour"})

    def test_usage_exact_log_shapes_and_original_observation_times(self):
        sub = self.subscription()
        path = Path(sub["config_dir"]) / "sessions/2026/10/19/rollout-one.jsonl"
        path.parent.mkdir(parents=True)
        stamp = "2026-10-19T00:00:00Z"
        event = {"timestamp": stamp, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "limit_id": "codex", "primary": {"used_percent": 24.0, "window_minutes": 10080,
            "resets_at": 1792908009}, "secondary": None, "credits": {"balance": "secret"}, "plan_type": "pro"}}}
        path.write_text("broken\n" + json.dumps(event) + "\n")
        # Parsing timestamps, not mtime: a newer transcript can contain an older observation.
        event["timestamp"] = "2026-10-18T00:00:00Z"
        event["payload"]["rate_limits"]["primary"]["used_percent"] = 8
        path.with_name("rollout-two.jsonl").write_text(json.dumps(event) + "\n")
        windows, read_at = self.m.usage_logs(sub)
        self.assertEqual(windows["seven_day"]["used_percent"], 24)
        self.assertEqual(read_at, self.m.usage_stamp(stamp))
        with mock.patch.object(self.m, "usage_codex", side_effect=OSError("TOKEN-MUST-NOT-LEAK")):
            _, reading = self.m.usage_reading(sub)
        self.assertEqual(reading["source"], "log")
        self.assertNotIn("TOKEN-MUST-NOT-LEAK", json.dumps(reading))
        sub = self.subscription("claude", runtime="claude")
        path = Path(sub["config_dir"]) / "projects/project/id.jsonl"; path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"timestamp": stamp, "apiError": "usage_limit_reached", "apiErrorParams": {
            "rate_limit_info": {"status": "rejected", "resetsAt": 1792395000, "rateLimitType": "five_hour"}}}) + "\n")
        windows, read_at = self.m.usage_logs(sub)
        self.assertEqual(windows["five_hour"]["used_percent"], 100)
        self.assertEqual(windows["five_hour"]["resets_at"], 1792395000)
        self.assertEqual(read_at, self.m.usage_stamp(stamp))
        self.assertNotIn("seven_day", windows)

    def test_usage_parser_defends_against_undocumented_shapes(self):
        self.assertEqual(self.m.usage_windows(None, "claude"), {})
        for value in (None, "20", float("nan"), -1, 101, True):
            self.assertEqual(self.m.usage_windows({"five_hour": {"utilization": value}}, "claude"), {})
        self.assertEqual(self.m.usage_windows({"primary": {"usedPercent": 20, "windowDurationMins": 1440}}, "codex"), {})
        self.assertEqual(self.m.usage_windows({"five_hour": {"used_percentage": 25, "resets_at": 1792395000}}, "claude")
                         ["five_hour"]["used_percent"], 25)
        self.assertIsNone(self.m.usage_stamp("2026-10-19T00:00:00"))

    def test_usage_atomic_write_and_cleanup_on_failure(self):
        path = self.m.usage_file()
        self.m.usage_atomic(path, {"before": True})
        with mock.patch.object(self.m.os, "replace", side_effect=OSError("fail")):
            with self.assertRaises(OSError): self.m.usage_atomic(path, {"after": True})
        self.assertEqual(json.loads(path.read_text()), {"before": True})
        self.assertEqual(list(path.parent.glob(".usage.json-*")), [])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_usage_poller_machine_lock_refresh_lock_and_exit(self):
        config = self.declare(self.subscription())
        with self.m.usage_lock("usage-watch.lock") as held:
            self.assertTrue(held)
            out = self.run_(["usage", "--watch", "--every", "0.1"], cwd=self.tmp, brain=CORE / "bin/brain")
            self.assertIn("poller already running", out)
        # Dead processes release flock regardless of a stale pid left in the file.
        self.m.usage_file("usage-watch.lock").write_text("999999")
        with self.m.usage_lock("usage-watch.lock") as held: self.assertTrue(held)
        with self.m.usage_lock("usage-refresh.lock"), mock.patch.object(self.m, "usage_reading") as lookup:
            self.m.usage_refresh(config)
            lookup.assert_not_called()
        args = type("Args", (), {"action": None, "watch": True, "every": 300})()
        with mock.patch.object(self.m, "usage_refresh") as refresh, \
                mock.patch.object(self.m.time, "sleep", side_effect=KeyboardInterrupt):
            self.m.cmd_usage(args)
            refresh.assert_called_once()
        with self.m.usage_lock("usage-watch.lock") as held: self.assertTrue(held)

    def test_usage_failed_lookup_preserves_newer_stop_observation(self):
        root = self.init("alice")
        config = self.declare(self.subscription())
        self.cache(config, [self.reading(90)], checked=time.time() - 180)
        stale_log = self.reading(10, source="log", age=86400)
        with mock.patch.object(self.m, "usage_reading", return_value=("path-key", stale_log)):
            selected, why = self.m.usage_route(self.m.Instance(root), ["codex"])
        self.assertIsNone(selected)
        self.assertIn("Stop starting jobs", why)
        rows, _ = self.m.usage_rows()
        self.assertEqual(rows[0]["source"], "lookup")
        self.assertEqual(rows[0]["windows"]["five_hour"]["used_percent"], 90)
        self.assertEqual(rows[0]["note"], "")

    def test_usage_stale_refresh_once_and_read_only_ui(self):
        root = self.init("alice")
        config = self.declare(self.subscription())
        self.cache(config, [self.reading()], checked=time.time() - 121)
        inst = self.m.Instance(root)
        with mock.patch.object(self.m, "usage_reading", return_value=("a", self.reading())) as lookup:
            self.m.usage_route(inst, inst.runtimes)
            self.m.usage_route(inst, inst.runtimes)
            self.assertEqual(lookup.call_count, 1)
            state = self.m.ui_state(inst)
            self.assertEqual(lookup.call_count, 1)
        self.assertEqual(state["usage"][0]["source"], "lookup")
        self.assertIsNotNone(state["usage"][0]["age_seconds"])
        self.cache(config, [self.reading()], checked=time.time() - 300)
        with mock.patch.object(self.m, "usage_reading") as lookup:
            self.m.usage_rows(inst)
            self.m.ui_state(inst)
            lookup.assert_not_called()

    def test_usage_routing_stop_pacing_allowlist_fallback_and_unknown(self):
        root = self.init("alice"); inst = self.m.Instance(root)
        one, two, work = self.subscription(), self.subscription("two"), self.subscription("work", brains=["work"])
        config = self.declare(one, two, work)
        self.cache(config, [self.reading(85), self.reading(40), self.reading(0)])
        selected, reason = self.m.usage_route(inst, ["codex"])
        self.assertEqual(selected["id"], "two")
        self.cache(config, [self.reading(10, weekly=85), self.reading(40), self.reading(0)])
        self.assertEqual(self.m.usage_route(inst, ["codex"])[0]["id"], "two")
        self.cache(config, [self.reading(10, weekly=30, elapsed=5), self.reading(40, weekly=40), self.reading(0)])
        self.assertEqual(self.m.usage_route(inst, ["codex"])[0]["id"], "two")
        self.cache(config, [self.reading(10, weekly=25), self.reading(40, weekly=40), self.reading(0)])
        self.assertEqual(self.m.usage_route(inst, ["codex"])[0]["id"], "one")
        self.cache(config, [self.reading(30), self.reading(30), self.reading(0)])
        inst.local["fallback"] = ["host-two", "host-one"]
        self.assertEqual(self.m.usage_route(inst, ["codex"])[0]["id"], "two")
        self.cache(config, [self.reading(source="unknown"), self.reading(25), self.reading(0)])
        self.assertEqual(self.m.usage_route(inst, ["codex"])[0]["id"], "two")
        self.cache(config, [self.reading(source="unknown"), self.reading(90), self.reading(0)])
        selected, reason = self.m.usage_route(inst, ["codex"])
        self.assertEqual(selected["id"], "one"); self.assertIn("unknown", reason)
        self.cache(config, [self.reading(85), self.reading(90), self.reading(0)])
        self.assertIsNone(self.m.usage_route(inst, ["codex"])[0])
        self.assertIn("Stop starting jobs", self.m.usage_route(inst, ["codex"])[1])
        config = self.declare(one, two, stop_percent=70)
        self.cache(config, [self.reading(70), self.reading(60)])
        self.assertEqual(self.m.usage_route(inst, ["codex"])[0]["id"], "two")
        r = self.reading(99); r["windows"]["five_hour"]["resets_at"] = time.time() - 1
        self.cache(config, [r, self.reading(90)])
        self.assertIn("unknown", self.m.usage_route(inst, ["codex"])[1])

    def test_usage_brief_hire_review_and_json_timezone(self):
        root = self.init("alice")
        config = self.declare(self.subscription(), self.subscription("two", runtime="claude"))
        self.cache(config, [self.reading(source="unknown"), self.reading(source="unknown")])
        self.b(root, "set", "root", "runtime=codex")
        self.assertIn("runtime: codex", self.b(root, "brief", "root"))
        local = root / ".brain/local.json"
        data = json.loads(local.read_text()); data["fallback"] = ["host-two", "host-one"]
        local.write_text(json.dumps(data))
        self.assertIn("host-one", self.b(root, "brief", "root"))
        self.b(root, "set", "root", "runtime=claude")
        self.cache(config, [self.reading(90), self.reading(35)])
        text = self.b(root, "brief", "root", "--task", "do it")
        self.assertIn("runtime: claude", text); self.assertIn("host-two", text)
        text = self.b(root, "brief", "root", "--runtime", "codex")
        self.assertIn("Stop starting jobs", text)
        self.b(root, "specialist", "new", "qa", "--title", "QA")
        self.assertIn("host-two", self.b(root, "hire", "qa", "root", "--task", "test"))
        self.git("branch", "job/test", cwd=root)
        text = self.b(root, "review", "root", "--branch", "job/test", "--built-by", "codex")
        self.assertIn("Reviewer runtime: claude", text); self.assertIn("host-two", text)
        text = self.b(root, "review", "root", "--branch", "job/test", "--built-by", "claude")
        self.assertIn("Stop starting jobs", text)
        file = root / "instance.md"; file.write_text(file.read_text().replace("timezone: UTC", "timezone: Asia/Beirut"))
        out = json.loads(self.b(root, "usage", "--json"))
        self.assertEqual(out["scope"], "account-wide")
        window = out["subscriptions"][0]["windows"]["five_hour"]
        reset = datetime.datetime.fromisoformat(window["reset_time"])
        expected = datetime.datetime.fromtimestamp(window["resets_at"], ZoneInfo("Asia/Beirut"))
        self.assertEqual(reset.utcoffset(), expected.utcoffset())
        self.assertEqual(reset.timestamp(), int(window["resets_at"]))
        self.assertNotIn("machine_share", out["subscriptions"][0])
        text = self.b(root, "usage")
        self.assertIn("Account-wide", text); self.assertIn("share: unavailable", text)
        self.assertIn("source", text)
        (self.cfg / "subscriptions.json").write_text("invalid")
        self.assertIn("Usage unknown", self.b(root, "brief", "root"))

    def test_usage_reset_timezone_across_dst_boundary(self):
        root = self.init("alice")
        file = root / "instance.md"
        file.write_text(file.read_text().replace("timezone: UTC", "timezone: Asia/Beirut"))
        inst = self.m.Instance(root)
        config = self.declare(self.subscription())
        zone = ZoneInfo("Asia/Beirut")
        # The second one-hour window crosses the rollback; reset uses winter time
        # even though the reading and current clock still use summer time.
        for hour, offset in ((19, 3), (20, 2)):
            now = datetime.datetime(2026, 10, 24, hour, 30, tzinfo=datetime.timezone.utc).timestamp()
            with self.subTest(hour=hour), mock.patch.object(self.m.time, "time", return_value=now):
                self.cache(config, [self.reading()])
                rows, _ = self.m.usage_rows(inst)
                window = rows[0]["windows"]["five_hour"]
                reset = datetime.datetime.fromisoformat(window["reset_time"])
                expected = datetime.datetime.fromtimestamp(window["resets_at"], zone)
                self.assertEqual(reset.isoformat(), expected.isoformat())
                self.assertEqual(reset.timestamp(), now + 3600)
                self.assertEqual(reset.utcoffset(), datetime.timedelta(hours=offset))
                self.assertEqual(datetime.datetime.fromtimestamp(now, zone).utcoffset(),
                                 datetime.timedelta(hours=3))

    def test_usage_corrupt_cache_is_unknown_and_ui_redacts_values_safely(self):
        root = self.init("alice")
        sub = self.subscription()
        config = self.declare(sub)
        self.m.usage_file().write_text('["invalid cache shape"]')
        self.assertEqual(self.m.usage_rows()[0][0]["source"], "unknown")
        data = self.cache(config, [self.reading()])
        next(iter(data["accounts"].values()))["windows"]["five_hour"]["used_percent"] = "invalid"
        self.m.usage_file().write_text(json.dumps(data))
        self.assertEqual(self.m.usage_rows()[0][0]["source"], "unknown")
        sub["label"] = 'Private "label"'
        config = self.declare(sub)
        self.cache(config, [self.reading()])
        (root / "never-name.txt").write_text('Private\n"\n')
        rows = self.m.usage_ui(self.m.Instance(root))
        self.assertNotIn("Private", rows[0]["label"])
        self.assertEqual(rows[0]["source"], "lookup")
        self.assertIn("windows", rows[0])

    def test_usage_poller_interval_and_locked_readers_do_not_poll(self):
        root = self.init("alice"); inst = self.m.Instance(root)
        config = self.declare(self.subscription())
        data = self.cache(config, [self.reading(age=280)], checked=time.time() - 280)
        data["poll_interval_seconds"] = 300
        self.m.usage_atomic(self.m.usage_file(), data)
        self.assertTrue(self.m.usage_cache_fresh(data, config))
        with mock.patch.object(self.m, "usage_reading") as lookup:
            self.assertNotIn("unknown/stale", self.m.usage_route(inst, ["codex"])[1])
            lookup.assert_not_called()
        data["checked_at"] = time.time() - 1000
        self.m.usage_atomic(self.m.usage_file(), data)
        before = self.m.usage_file().read_bytes()
        with self.m.usage_lock("usage-watch.lock"), mock.patch.object(self.m, "usage_reading") as lookup:
            self.m.usage_route(inst, ["codex"])
            self.m.usage_rows(inst, before_launch=True)
            self.assertTrue(self.m.usage_poller_live())
            lookup.assert_not_called()
        self.assertFalse(self.m.usage_poller_live())
        self.assertEqual(self.m.usage_file().read_bytes(), before)

    def test_usage_failed_inline_refresh_never_rewrites_good_cache(self):
        root = self.init("alice"); inst = self.m.Instance(root)
        config = self.declare(self.subscription())
        data = self.cache(config, [self.reading(35, weekly=55)], checked=time.time() - 500)
        before = self.m.usage_file().read_bytes()
        for failed in (self.reading(source="unknown"), self.reading(10, source="log", age=86400)):
            with mock.patch.object(self.m, "usage_reading", return_value=("path-key", failed)):
                self.m.usage_route(inst, ["codex"])
            self.assertEqual(self.m.usage_file().read_bytes(), before)
            self.assertEqual(self.m.usage_cache()["checked_at"], data["checked_at"])

    def test_usage_window_merge_survives_newer_sparse_log(self):
        config = self.declare(self.subscription())
        old = self.reading(10, weekly=40, age=60)
        data = self.cache(config, [old])
        new = self.reading(90, source="log")
        with mock.patch.object(self.m, "usage_reading", return_value=("path-key", new)):
            out = self.m.usage_refresh(config, poll_interval=300)
        reading = out["accounts"][data["subscriptions"][0]["account_key"]]
        self.assertEqual(reading["windows"]["five_hour"]["used_percent"], 90)
        self.assertEqual(reading["windows"]["five_hour"]["source"], "log")
        self.assertEqual(reading["windows"]["seven_day"]["used_percent"], 40)
        self.assertEqual(reading["windows"]["seven_day"]["source"], "lookup")
        self.assertEqual(reading["windows"]["seven_day"]["read_at"], old["read_at"])
        self.assertEqual(out["poll_interval_seconds"], 300)
        self.assertTrue(self.m.usage_cache())

    def test_usage_watch_recovers_bad_cycle_without_error_details(self):
        import contextlib
        import io
        args = type("Args", (), {"action": None, "watch": True, "every": 300})()
        stderr = io.StringIO()
        with mock.patch.object(self.m, "usage_refresh", side_effect=[TypeError("SECRET"), {}]) as refresh, \
                mock.patch.object(self.m.time, "sleep", side_effect=[None, KeyboardInterrupt]), \
                contextlib.redirect_stderr(stderr):
            self.m.cmd_usage(args)
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(refresh.call_args.kwargs["poll_interval"], 300)
        self.assertIn("retrying", stderr.getvalue())
        self.assertNotIn("SECRET", stderr.getvalue())
        with self.m.usage_lock("usage-watch.lock") as held:
            self.assertTrue(held)

    def test_usage_unconfigured_or_disallowed_never_stops_or_changes_runtime(self):
        root = self.init("alice"); inst = self.m.Instance(root)
        self.b(root, "set", "root", "runtime=codex")
        with mock.patch.object(self.m.shutil, "which", return_value=None), \
                mock.patch.object(self.m, "usage_reading") as lookup:
            selected, reason = self.m.usage_route(inst, ["codex"])
            self.assertIsNone(selected); self.assertIn("Usage unknown", reason)
            self.assertNotIn("Stop", reason); lookup.assert_not_called()
        self.declare(self.subscription(runtime="claude", brains=["work"]))
        for rt in ("codex", "claude", "other"):
            selected, reason = self.m.usage_route(inst, [rt])
            self.assertIsNone(selected); self.assertIn("Usage unknown", reason)
            self.assertNotIn("Stop", reason)
        text = self.b(root, "brief", "root")
        self.assertIn("runtime: codex", text); self.assertNotIn("Stop starting", text)
        (self.cfg / "subscriptions.json").unlink()
        self.assertIn("runtime: codex", self.b(root, "brief", "root"))

    def test_usage_visibility_json_ui_and_autodetected_defaults(self):
        root = self.init("alice"); inst = self.m.Instance(root)
        own = self.subscription()
        hidden = self.subscription("SECRET-LABEL", runtime="claude", brains=["SECRET-BRAIN"])
        config = self.declare(own, hidden)
        self.cache(config, [self.reading(), self.reading()])
        out = json.loads(self.b(root, "usage", "--json"))
        self.assertEqual(out["other_subscriptions"], 1)
        self.assertEqual([r["id"] for r in out["subscriptions"]], ["one"])
        self.assertNotIn("SECRET", json.dumps(out))
        ui = self.m.ui_state(inst)
        self.assertEqual(ui["usage_other_subscriptions"], 1)
        self.assertNotIn("SECRET", json.dumps(ui["usage"]))
        self.assertNotIn("config_dir", json.dumps(ui["usage"]))
        self.assertNotIn(own["config_dir"], json.dumps(ui["usage"]))
        (self.cfg / "subscriptions.json").unlink()
        with mock.patch.object(self.m.shutil, "which", return_value="installed"):
            rows, _ = self.m.usage_rows(inst)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["available_to_every_brain"] and r["autodetected"] for r in rows))

    def test_usage_scoped_weekly_routes_only_matching_model(self):
        root = self.init("alice"); inst = self.m.Instance(root)
        config = self.declare(self.subscription(runtime="claude"))
        reading = self.reading(25, weekly=50)
        reading["windows"]["seven_day_model:Fable"] = {"used_percent": 90,
            "resets_at": time.time() + 86400, "models": ["fable-id", "Fable"]}
        self.cache(config, [reading])
        for model in (None, "other"):
            self.assertIsNotNone(self.m.usage_route(inst, ["claude"], model=model)[0])
        for model in ("fable", "fable-id"):
            self.assertIn("Stop starting", self.m.usage_route(inst, ["claude"], model=model)[1])
        text = self.b(root, "brief", "root", "--runtime", "claude", "--model", "Fable")
        self.assertIn("Stop starting", text); self.assertIn("model: Fable", text)
        self.assertIn("weekly model fable-id, Fable", self.b(root, "usage"))

    def test_usage_pinned_brief_names_alternative_without_switching(self):
        root = self.init("alice")
        self.b(root, "set", "root", "runtime=claude")
        config = self.declare(self.subscription(runtime="claude"), self.subscription("two"))
        self.cache(config, [self.reading(40), self.reading(10)])
        text = self.b(root, "brief", "root")
        self.assertIn("runtime: claude", text); self.assertIn("host-one", text)
        self.assertNotIn("host-two", text)
        self.cache(config, [self.reading(90), self.reading(10)])
        text = self.b(root, "brief", "root")
        self.assertIn("runtime: claude", text); self.assertIn("Stop starting", text)
        self.assertIn("Pinned runtime claude", text); self.assertIn("Alternative: two (codex)", text)
        text = self.b(root, "brief", "root", "--runtime", "codex")
        self.assertIn("runtime: codex", text); self.assertIn("host-two", text)

    def test_import_brief_composes_worker_usage_limits_and_handback(self):
        root = self.init("alice")
        home = self.repo(self.tmp / "project")
        self.b(root, "adopt", str(home))
        self.b(root, "set", "project", "runtime=claude", "coordinator=study-owner")
        node = root / "nodes/project"
        (node / "brief.md").write_text("## Limits\n\n- Stay offline. Why: private inputs. Lift when: never.\n")
        config = self.declare(self.subscription(runtime="claude"), self.subscription("two"))
        self.cache(config, [self.reading(90), self.reading(10)])
        before = self.git("status", "--porcelain", cwd=home), self.git("rev-parse", "HEAD", cwd=home)
        text = self.b(root, "brief", "project", "--import", "--model", "opus")
        for expected in ("runtime: claude", "model: opus", "Stop starting jobs", "Pinned runtime claude",
                         "Alternative: two (codex)", "study-owner", "brain handback project --branch import:",
                         "no home ready evidence", "source branch and history unchanged"):
            self.assertIn(expected, text)
        for section in ("Task", "Limits", "Before you hand back", "Questions and hand-back", "Report"):
            self.assertEqual(text.count("## " + section + "\n"), 1, section)
        self.assertEqual(text.count("Stay offline."), 1)
        self.assertNotIn("Run `brain guard", text)
        self.assertNotIn("brain ready", text)
        self.assertNotIn("must have a Lessons section", text)
        text = self.b(root, "brief", "project", "--import", "--runtime", "codex", "--model", "opus")
        self.assertIn("runtime: codex", text)
        self.assertIn("host-two", text)
        self.assertNotIn("Stop starting jobs", text)
        self.assertEqual(before, (self.git("status", "--porcelain", cwd=home),
                                  self.git("rev-parse", "HEAD", cwd=home)))

    def test_proposed_hire_gate_keeps_usage_model_and_job_record(self):
        root = self.init("alice")
        self.b(root, "set", "root", "runtime=codex")  # node pins now take precedence over specialist defaults
        config = self.declare(self.subscription())
        reading = self.reading(10)
        reading["windows"]["seven_day_model:opus"] = {"used_percent": 95, "resets_at": time.time() + 86400, "models": ["opus"]}
        self.cache(config, [reading])
        self.b(root, "specialist", "new", "qa", "--title", "QA", "--runtime", "codex", "--proposed")
        out = self.b(root, "hire", "qa", "root", "--task", "Inspect", "--model", "opus",
                     "--job", "job/import", ok=False)
        self.assertIn("imported specialist is unreviewed", out)
        local = root / ".brain/local.json"
        self.assertFalse(json.loads(local.read_text()).get("hires") if local.exists() else False)
        self.b(root, "specialist", "accept", "qa", "--reviewed-by", "alice")
        out = self.b(root, "hire", "qa", "root", "--task", "Inspect", "--model", "opus", "--job", "job/import")
        self.assertIn("model: opus", out)
        self.assertIn("Stop starting jobs", out)
        self.assertEqual(json.loads((root / ".brain/local.json").read_text())["hires"][-1]["job"], "job/import")

    def test_usage_human_output_and_unknown_zone_keep_offset(self):
        root = self.init("alice"); inst = self.m.Instance(root)
        inst.zone = lambda: None
        config = self.declare(self.subscription(), self.subscription("long-label"))
        self.cache(config, [self.reading(age=28080), self.reading()])
        row = self.m.usage_rows(inst)[0][0]
        self.assertRegex(row["reading_time"], r"[+-]\d\d:\d\d$")
        self.assertRegex(self.m.usage_time(time.time(), inst), r"^\d{4}-\d\d-\d\d \d\d:\d\d [+-]\d\d:\d\d$")
        self.assertEqual(self.m.usage_age(28080), "7 h 48 m")
        text = self.b(root, "usage")
        self.assertIn("7 h 48 m", text)
        lines = text.splitlines()[1:4]
        starts = [line.index("5h used") if i == 0 else line.index("20%") for i, line in enumerate(lines)]
        self.assertEqual(len(set(starts)), 1)
        self.assertNotRegex(text, r"\d{4}-\d\d-\d\dT")
        self.assertRegex(json.loads(self.b(root, "usage", "--json"))["subscriptions"][0]["reading_time"],
                         r"\d{4}-\d\d-\d\dT")

    def test_usage_launchagent_only_writes_requested_temp_path(self):
        import plistlib
        path = self.tmp / "agents/usage.plist"
        out = self.run_(["usage", "install", "--launch-agent", str(path)], cwd=self.tmp, brain=CORE / "bin/brain")
        data = plistlib.loads(path.read_bytes())
        self.assertIn("not loaded", out)
        self.assertEqual(data["EnvironmentVariables"]["BRAIN_CONFIG"], str(self.cfg / "brains.json"))
        self.assertEqual(data["ProgramArguments"][-4:], ["usage", "--watch", "--every", "300"])
        self.assertIn("--watch", data["ProgramArguments"])
        self.assertEqual(data["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(data["ThrottleInterval"], 30)
        self.assertEqual(data["ProcessType"], "Background")
        self.assertEqual(data["StandardOutPath"], str(self.tmp / "Library/Logs/brain-usage.log"))
        self.assertEqual(data["StandardOutPath"], data["StandardErrorPath"])
        self.assertFalse((self.tmp / "Library/LaunchAgents").exists())
        again = self.run_(["usage", "install", "--launch-agent", str(path)], cwd=self.tmp, brain=CORE / "bin/brain")
        self.assertIn("unchanged", again)
        self.assertEqual(plistlib.loads(path.read_bytes()), data)
        self.assertIn("removed", self.run_(["usage", "uninstall", "--launch-agent", str(path)], cwd=self.tmp,
                                           brain=CORE / "bin/brain"))
        self.assertFalse(path.exists())
        self.assertIn("not installed", self.run_(["usage", "uninstall", "--launch-agent", str(path)], cwd=self.tmp,
                                                 brain=CORE / "bin/brain"))

    def test_usage_install_replaces_only_its_own_agent(self):
        import plistlib
        path = self.tmp / "agents/usage.plist"
        path.parent.mkdir(parents=True)
        path.write_bytes(plistlib.dumps({"Label": "com.example.other"}))
        self.assertIn("isn't the brain usage LaunchAgent", self.run_(
            ["usage", "install", "--launch-agent", str(path)], cwd=self.tmp, ok=False, brain=CORE / "bin/brain"))
        self.assertEqual(plistlib.loads(path.read_bytes()), {"Label": "com.example.other"})
        path.write_bytes(plistlib.dumps({"Label": "org.brain.usage", "ProgramArguments": ["/old/core/bin/brain"]}))
        self.assertIn("wrote", self.run_(["usage", "install", "--launch-agent", str(path), "--every", "120"],
                                         cwd=self.tmp, brain=CORE / "bin/brain"))
        self.assertEqual(plistlib.loads(path.read_bytes())["ProgramArguments"][-1], "120")

    def fake_launchd(self, stops=True, starts=True, restarts=True):
        calls, state = [], {"loaded": False}
        def run(*args):
            calls.append(args[0])
            ok = {"print": state["loaded"], "bootout": stops, "bootstrap": starts, "kickstart": restarts}[args[0]]
            if args[0] == "bootout" and stops:
                state["loaded"] = False
            if args[0] == "bootstrap" and starts:
                state["loaded"] = True
            return subprocess.CompletedProcess(args, 0 if ok else 5, "", "" if ok else "Input/output error")
        for target, value in (("usage_launchd", run), ("usage_service_managed", lambda: True)):
            patcher = mock.patch.object(self.m, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (mock.patch.object(self.m.time, "sleep", lambda s: None),
                        mock.patch.object(self.m.sys, "platform", "darwin")):
            patcher.start()
            self.addCleanup(patcher.stop)
        return calls, state

    def usage_args(self, **kw):
        return argparse.Namespace(**{"launch_agent": None, "every": 300, "no_load": False, **kw})

    def test_usage_install_loads_once_and_reloads_only_on_change(self):
        calls, state = self.fake_launchd()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.usage_install(self.usage_args())
            self.m.usage_install(self.usage_args())
            self.m.usage_install(self.usage_args(every=120))
        self.assertTrue(state["loaded"])
        self.assertEqual([c for c in calls if c != "print"], ["bootstrap", "bootout", "bootstrap"])
        self.assertIn("already running", out.getvalue())

    def test_usage_failed_stop_never_reports_success(self):
        calls, state = self.fake_launchd(stops=False)
        agent = self.tmp / "Library/LaunchAgents/org.brain.usage.plist"
        with contextlib.redirect_stdout(io.StringIO()):
            self.m.usage_install(self.usage_args())
        with self.assertRaises(SystemExit) as e:
            self.m.usage_install(self.usage_args(every=120))
        self.assertIn("didn't stop", str(e.exception))
        with self.assertRaises(SystemExit) as e:
            self.m.usage_uninstall(self.usage_args())
        self.assertIn("didn't stop", str(e.exception))
        self.assertTrue(agent.exists(), "kept while the poller still runs")

    def test_usage_restart_only_for_this_brains_core_and_reports_failure(self):
        calls, state = self.fake_launchd(restarts=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.m.usage_install(self.usage_args())
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
            self.m.usage_restart(self.tmp / "other-brain")
        self.assertNotIn("kickstart", calls)
        self.assertEqual(out.getvalue() + err.getvalue(), "")
        import plistlib
        agent = self.tmp / "Library/LaunchAgents/org.brain.usage.plist"
        data = plistlib.loads(agent.read_bytes())
        data["ProgramArguments"][1] = str(self.tmp / "mine/core/bin/brain")
        agent.write_bytes(plistlib.dumps(data))
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
            self.m.usage_restart(self.tmp / "mine")
        self.assertIn("kickstart", calls)
        self.assertIn("couldn't restart", err.getvalue())
        self.assertNotIn("restarted", out.getvalue())

    def test_usage_install_default_path_never_loads_under_tests(self):
        import plistlib
        out = self.run_(["usage", "install"], cwd=self.tmp, brain=CORE / "bin/brain")
        if sys.platform != "darwin":
            self.assertIn("macOS-only", out)
            return
        path = self.tmp / "Library/LaunchAgents/org.brain.usage.plist"
        self.assertIn("not loaded", out)
        self.assertEqual(plistlib.loads(path.read_bytes())["Label"], "org.brain.usage")



class RoundTwoUsage(Scenario):
    def setUp(self):
        super().setUp()
        self.cfg = self.tmp / "config"
        self.cfg.mkdir()
        self.env["BRAIN_CONFIG"] = str(self.cfg / "brains.json")
        self.patch_env = mock.patch.dict(os.environ, self.env, clear=True)
        self.patch_env.start()
        self.addCleanup(self.patch_env.stop)
        loader = core_loader.SourceFileLoader("brain_r2_usage_test", str(CORE / "bin/brain"))
        self.m = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.m)

    subscription = Usage.subscription
    declare = Usage.declare
    cache = Usage.cache
    reading = Usage.reading

    def test_no_reading_yet_has_action_instead_of_unknown_rows(self):
        r = self.init("alice")
        self.declare(self.subscription())
        out = self.b(r, "usage")
        self.assertIn("no reading yet: run `brain usage --refresh`", out)
        self.assertIn("brain usage install", out)
        self.assertNotIn("unknown", out)
        self.assertEqual(json.loads(self.b(r, "usage", "--json"))["subscriptions"][0]["windows"], {})

    def test_budget_compares_only_enough_ledger_data_to_fresh_routed_window(self):
        r = self.init("alice")
        self.b(r, "new", "app", "--title", "App", "--runtime", "codex")
        inst = self.m.Instance(r); n = inst.node("app")
        def row(branch, hours, ended=True):
            return {**dict.fromkeys(self.m.JOB_COLS, ""), "Job": branch, "Started": "2026-01-01T00:00Z",
                    "Ended": "2026-01-01T01:00Z" if ended else "", "Outcome": "merged" if ended else "", "Hours": hours, "Runtime": "codex"}
        rows = [row("feat/" + str(i), ".2") for i in range(3)] + [row("feat/open", "", False)]
        self.m.write_jobs(inst, n, rows)
        cfg = self.declare(self.subscription())
        self.cache(cfg, [self.reading(20)])
        with mock.patch.object(self.m, "usage_refresh", side_effect=AssertionError("read-only budget polled")):
            lines = "\n".join(self.m.job_budget_lines(inst, n))
            self.assertIn("fits", lines)
            self.assertIn("allowance fit unknown", lines)
            reading = self.reading(20)
            reading["windows"]["five_hour"]["resets_at"] = time.time() + 900
            self.cache(cfg, [reading])
            self.assertIn("exceeds", "\n".join(self.m.job_budget_lines(inst, n, "feat/planned")))
        self.assertIn("typical serial full-job time", self.b(r, "usage"))
        self.assertIn("typical serial full-job time", self.b(r, "jobs", "app"))
        self.cache(cfg, [self.reading(20, age=1000)])
        self.assertEqual(self.m.job_budget_lines(inst, n), [])
        self.cache(cfg, [self.reading(source="unknown")])
        self.assertEqual(self.m.job_budget_lines(inst, n), [])
        self.cache(cfg, [self.reading(95)])
        self.assertEqual(self.m.job_budget_lines(inst, n), [])
        self.cache(cfg, [self.reading(20)])
        self.m.write_jobs(inst, n, rows[1:])
        self.assertEqual(self.m.job_budget_lines(inst, n), [])



if __name__ == "__main__":
    unittest.main()
