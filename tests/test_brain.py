"""Scenario tests for core/bin/brain. Run with `brain selftest` (or python3 -m unittest in tests/).

Every test builds instances in a throwaway folder that is also $HOME, so nothing real is touched.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

CORE = Path(os.environ.get("BRAIN_CORE") or Path(__file__).resolve().parent.parent)


class Scenario(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="brain-test-")).resolve()
        self.env = {**os.environ, "HOME": str(self.tmp), "BRAIN_NO_REEXEC": "1",
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                    "PATH": os.environ.get("PATH", "")}
        for k in ("BRAIN_ROOT", "BRAIN_CONFIG", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
            self.env.pop(k, None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def run_(self, args, cwd, ok=True, brain=None):
        r = subprocess.run([sys.executable, str(brain or (cwd / "core" / "bin" / "brain")), *args], cwd=cwd,
                           env=self.env, capture_output=True, text=True)
        if ok is True and r.returncode:
            self.fail(f"brain {' '.join(args)} failed:\n{r.stdout}\n{r.stderr}")
        if ok is False and not r.returncode:
            self.fail(f"brain {' '.join(args)} should have failed:\n{r.stdout}")
        return r.stdout + r.stderr

    def git(self, *args, cwd):
        r = subprocess.run(["git", "-c", "protocol.file.allow=always", *args], cwd=cwd, env=self.env,
                           capture_output=True, text=True)
        if r.returncode:
            self.fail(f"git {' '.join(args)}: {r.stderr}")
        return r.stdout

    def init(self, name, operator=None):
        root = self.tmp / name
        self.run_(["init", str(root), "--operator", operator or name, "--title", f"{name} brain",
                   "--tz", "UTC"], cwd=self.tmp, brain=CORE / "bin" / "brain")
        return root

    def b(self, root, *args, ok=True):
        return self.run_(list(args), cwd=root, ok=ok)

    def repo(self, path, files=None):
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main", cwd=path)
        for k, v in (files or {"README.md": "hello\n"}).items():
            (path / k).write_text(v)
        self.git("add", "-A", cwd=path)
        self.git("commit", "-qm", "init", cwd=path)
        return path

    def all_text(self, folder):
        """Everything an instance holds, except the core (which is the same everywhere)."""
        out = []
        for p in Path(folder).rglob("*"):
            if p.is_file() and ".git" not in p.parts and "core" not in p.relative_to(folder).parts:
                try:
                    out.append(p.read_text(errors="ignore"))
                except OSError:
                    pass
        return "\n".join(out)


class Basics(Scenario):
    def test_fresh_instance_is_generic_and_passes(self):
        r = self.init("alice")
        self.assertIn("✓", self.b(r, "check"))
        self.assertIn("root: alice brain  owners=alice", self.b(r, "tree"))
        inst = (r / "instance.md").read_text()
        self.assertIn("principals: alice", inst)
        self.assertTrue((r / "CLAUDE.md").is_symlink())
        self.assertTrue((r / ".agents" / "skills").is_symlink())
        self.assertIn(".brain/", (r / ".gitignore").read_text())

    def test_nesting_and_relabel_touch_only_labels(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A", "--labels", "area:x")
        self.b(r, "new", "a/b/", "--title", "B")
        self.b(r, "new", "a/b/c", "--title", "C")
        self.assertIn("      a/b/c: C", self.b(r, "tree"))
        head = self.git("rev-parse", "HEAD", cwd=r).strip()
        self.b(r, "relabel", "a", "area:y", "kind:z")
        changed = self.git("diff", "--name-only", head, "HEAD", cwd=r).split()
        self.assertEqual(sorted(changed), ["nodes/a/node.md", "registry.md"])

    def test_bare_brain_is_a_dashboard(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        with open(r / "nodes" / "a" / "inbox.md", "a") as fh:
            fh.write("- 2026-01-01 10:00 from worker: look at the invoice\n")
        out = self.b(r)
        self.assertIn("Needs attention", out)
        self.assertIn("look at the invoice", out)
        self.assertIn("no coordinator on this machine", out)

    def test_principal_add_and_unknown_principals_refused(self):
        r = self.init("alice")
        self.b(r, "new", "x", "--title", "X", "--owners", "bob", ok=False)
        self.b(r, "principal", "add", "bob")
        self.assertIn("bob", self.b(r, "principal", "list"))


class MachineBindings(Scenario):
    def test_host_ids_stay_on_this_machine(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        head = self.git("rev-parse", "HEAD", cwd=r).strip()
        self.b(r, "set", "a", "coordinator=thread-123", "host_project=proj-9")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=r).strip(), head, "nothing to commit")
        local = json.loads((r / ".brain" / "local.json").read_text())
        self.assertEqual(local["nodes"]["a"]["coordinator"], "thread-123")
        self.assertNotIn("thread-123", (r / "nodes" / "a" / "node.md").read_text())
        self.assertNotIn("thread-123", (r / "registry.md").read_text())
        self.assertIn("t3-thread://v1/thread-123", self.b(r, "show", "a"))
        self.assertEqual(self.git("status", "--porcelain", cwd=r).strip(), "")

    def test_legacy_bindings_in_node_md_are_migrated_by_fix(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        nm = r / "nodes" / "a" / "node.md"
        nm.write_text(nm.read_text().replace("created:", "coordinator: old-thread\ncreated:"))
        self.git("commit", "-qam", "legacy", cwd=r)
        self.assertIn("machine-specific", self.b(r, "check", ok=False))
        self.assertIn("fixed:", self.b(r, "check", "--fix"))
        self.assertNotIn("coordinator", nm.read_text().split("---")[1])
        self.assertIn("old-thread", (r / ".brain" / "local.json").read_text())

    def test_move_carries_bindings_and_pins_owners(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        self.b(r, "new", "a", "--title", "A")
        (self.tmp / "t-home").mkdir()
        self.b(r, "new", "t", "--title", "T", "--owners", "team", "--home", str(self.tmp / "t-home"))
        self.b(r, "set", "a", "coordinator=c-a")
        out = self.b(r, "move", "a", "t")
        self.assertIn("owners kept as alice", out)
        self.assertIn("SEALED", out)
        self.assertIn("c-a", json.dumps(json.loads((r / ".brain" / "local.json").read_text())["nodes"]["t/a"]))
        self.assertIn("Node `t/a`", (r / "nodes" / "t" / "nodes" / "a" / "AGENTS.md").read_text())


class Sealing(Scenario):
    def test_seal_moves_shared_material_out_and_leaves_no_trace(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "bob")
        self.b(r, "new", "venture", "--title", "Venture")
        (r / "nodes" / "venture" / "sources" / "plan.md").write_text("the shared plan\n")
        (r / "nodes" / "venture" / "status.md").write_text("Updated: x\nPRIVATE-ALICE thoughts\n")
        self.git("add", "-A", cwd=r); self.git("commit", "-qm", "work", cwd=r)
        home = self.tmp / "code" / "venture"
        out = self.b(r, "seal", "venture", "--owners", "alice,bob", "--home", str(home), "--move", "sources/plan.md")
        self.assertIn("initial commit", out)
        self.assertEqual((home / "sources" / "plan.md").read_text(), "the shared plan\n")
        self.assertFalse((r / "nodes" / "venture" / "sources" / "plan.md").exists())
        self.assertTrue((home / "CLAUDE.md").is_symlink())
        self.assertNotIn("PRIVATE-ALICE", self.all_text(home))
        nm = (r / "nodes" / "venture" / "node.md").read_text()
        self.assertIn("home: external", nm)
        self.assertNotIn(str(home), nm)
        self.assertIn("operator's side of a sealed node", (r / "nodes" / "venture" / "AGENTS.md").read_text())
        self.assertNotIn(".brain", self.git("ls-files", cwd=r))
        self.assertIn("✓", self.b(r, "check"))

    def test_traces_are_found_in_any_spelling_tracked_or_not(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "bob")
        self.b(r, "new", "v", "--title", "V")
        home = self.tmp / "vh"
        self.b(r, "seal", "v", "--owners", "bob", "--home", str(home))
        (home / "notes.md").write_text("see ~/alice/nodes for context\n")  # untracked, tilde form
        self.assertIn("trace of '~/alice'", self.b(r, "check", ok=False))
        (home / "notes.md").write_text(f"see {r} for context\n")
        self.assertIn("axiom 6", self.b(r, "check", ok=False))

    def test_seal_refuses_traces_from_moved_files(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "bob")
        self.b(r, "new", "v", "--title", "V")
        (r / "nodes" / "v" / "sources" / "leak.md").write_text(f"from {r}/nodes/v\n")
        home = self.tmp / "vh"
        out = self.b(r, "seal", "v", "--owners", "alice,bob", "--home", str(home), "--move", "sources/leak.md", ok=False)
        self.assertIn("upward traces", out)
        self.assertEqual(self.git("rev-list", "--all", cwd=home).strip(), "", "nothing committed in the home")

    def test_sealed_node_needs_an_external_home(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "bob")
        self.b(r, "new", "v", "--title", "V")
        self.b(r, "owners", "v", "bob")
        self.assertIn("brain seal", self.b(r, "check", ok=False))
        self.b(r, "seal", "v", "--owners", "alice", "--home", str(self.tmp / "x"), ok=False)  # same as parent


class Collaboration(Scenario):
    """Two operators, each with a Brain, working on one shared repository."""

    def test_two_brains_meet_only_in_the_shared_repo(self):
        shared = self.tmp / "shared.git"
        self.git("init", "-q", "--bare", "-b", "main", str(shared), cwd=self.tmp)
        seed = self.repo(self.tmp / "seed")
        self.git("remote", "add", "origin", str(shared), cwd=seed)
        self.git("push", "-q", "origin", "main", cwd=seed)

        brains = {}
        for me, other in (("ana", "ben"), ("ben", "ana")):
            r = self.init(me)
            self.b(r, "principal", "add", other)
            clone = self.tmp / f"{me}-code" / "proj"
            self.git("clone", "-q", str(shared), str(clone), cwd=self.tmp)
            out = self.b(r, "new", "proj", "--title", "Proj", "--owners", f"{me},{other}", "--home", str(clone))
            self.assertIn("(sealed)", out)
            self.assertIn(f"home_url: {shared}", (r / "nodes" / "proj" / "node.md").read_text())
            st = r / "nodes" / "proj" / "status.md"
            st.write_text(f"Updated: 2026-01-01 09:00 by proj coordinator (claude)\nPRIVATE-{me.upper()} opinion\n")
            self.git("commit", "-qam", "proj: status", cwd=r)
            brains[me] = (r, clone)

        # ana's worker does a job in the shared repo, on a branch
        hr, hclone = brains["ana"]
        self.git("checkout", "-q", "-b", "auth-tests", cwd=hclone)
        (hclone / "test_auth.py").write_text("def test_guard():\n    assert True\n")
        self.git("add", "-A", cwd=hclone); self.git("commit", "-qm", "Add auth guard tests", cwd=hclone)
        self.git("push", "-q", "origin", "auth-tests", cwd=hclone)

        # ben's Brain learns about it only through the repo
        rr, rclone = brains["ben"]
        out = self.b(rr, "sync", "proj")
        self.assertIn("Add auth guard tests", out)
        self.assertIn("auth-tests", out)
        time.sleep(1.1)  # git dates have one-second resolution
        self.b(rr, "sync", "proj", "--mark")
        self.assertNotIn("Add auth guard tests", self.b(rr, "sync", "proj", "--offline"))

        # neither Brain leaks into the repo or into the other Brain
        refs = self.git("for-each-ref", "--format=%(refname)", cwd=shared).split()
        for me, (r, _) in brains.items():
            for needle in (str(r), f"~/{me}", f"PRIVATE-{me.upper()}"):
                g = subprocess.run(["git", "grep", "-i", "-F", "-e", needle, *refs], cwd=shared,
                                   capture_output=True, text=True, env=self.env)
                self.assertEqual(g.stdout, "", f"{needle} leaked into the shared repo")
        self.assertNotIn("PRIVATE-ANA", self.all_text(rr))
        self.assertNotIn("PRIVATE-BEN", self.all_text(hr))
        for r, _ in brains.values():
            self.assertIn("✓", self.b(r, "check"))

    def test_scope_of_knowledge_learned_from_a_shared_node(self):
        r = self.init("ana")
        for p in ("ben", "cleo", "team"):
            self.b(r, "principal", "add", p)
        for nid, owners in (("proj", "ana,ben"), ("other", "ana,cleo"), ("work", "team")):
            (self.tmp / f"{nid}-home").mkdir()
            self.b(r, "new", nid, "--title", nid, "--owners", owners, "--home", str(self.tmp / f"{nid}-home"))
        self.b(r, "new", "solo", "--title", "Solo")
        self.b(r, "item", "add", "guard-method", "--from", "proj", "--title", "Guard tests", "--kind", "method",
               "--ref", "shared.git@abc123", "--body", "Test the guard both ways.")
        self.b(r, "usable", "guard-method", "proj")
        self.b(r, "usable", "guard-method", "solo")           # nobody new gains access
        self.b(r, "usable", "guard-method", "other", ok=False)  # cleo isn't in scope
        self.b(r, "usable", "guard-method", "work", ok=False)   # team isn't in scope
        self.b(r, "item", "scope", "guard-method", "*", ok=False)
        self.b(r, "item", "scope", "guard-method", "*", "--consented-by", "ana", ok=False)
        self.b(r, "item", "scope", "guard-method", "*", "--consented-by", "ana,ben")
        self.b(r, "usable", "guard-method", "work")
        self.assertIn("✓", self.b(r, "check"))


class Transitions(Scenario):
    def test_split_and_merge_keep_provenance(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        self.b(r, "new", "a/c", "--title", "C")
        (r / "nodes" / "a" / "sources" / "brand.md").write_text("brand notes\n")
        self.b(r, "item", "add", "palette", "--from", "a", "--title", "Palette", "--kind", "style", "--ref", "a/sources")
        self.b(r, "split", "a", "brand", "--title", "Brand", "--move", "sources/brand.md")
        self.assertTrue((r / "nodes" / "brand" / "sources" / "brand.md").exists())
        self.assertIn("split", (r / "nodes" / "brand" / "inbox.md").read_text())
        self.b(r, "new", "d", "--title", "D")
        self.b(r, "merge", "a", "d")
        self.assertFalse((r / "nodes" / "a").exists())
        self.assertTrue((r / "nodes" / "d" / "nodes" / "c" / "node.md").exists())
        self.assertTrue((r / "nodes" / "d" / "sources" / "merged-a" / "brief.md").exists())
        self.assertIn("merged_from: a", (r / "nodes" / "d" / "node.md").read_text())
        self.assertIn("source_node: a", next((r / "library").glob("palette.md")).read_text())
        self.assertIn("✓", self.b(r, "check"))

    def test_merge_refuses_across_owners(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "bob")
        self.b(r, "new", "a", "--title", "A")
        (self.tmp / "bh").mkdir()
        self.b(r, "new", "b", "--title", "B", "--owners", "bob", "--home", str(self.tmp / "bh"))
        self.assertIn("owners differ", self.b(r, "merge", "a", "b", ok=False))

    def test_revoke_quarantines_and_archives(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "vendor")
        (self.tmp / "vh").mkdir()
        self.b(r, "new", "contract", "--title", "Contract", "--owners", "vendor", "--home", str(self.tmp / "vh"))
        self.b(r, "item", "add", "rates", "--from", "contract", "--title", "Rates", "--kind", "fact", "--ref", "x")
        self.b(r, "item", "add", "mine", "--from", "root", "--title", "Mine", "--kind", "fact", "--ref", "y")
        out = self.b(r, "revoke", "vendor")
        self.assertIn("quarantined items: rates", out)
        self.assertIn("archived nodes: contract", out)
        self.assertIn("quarantined", self.b(r, "usable", "rates", "root", ok=False))
        self.b(r, "usable", "mine", "root")
        self.b(r, "new", "x", "--title", "X", "--owners", "vendor", ok=False)
        self.assertIn("✓", self.b(r, "check"))


class Specialists(Scenario):
    def test_hire_brings_only_the_lessons_in_scope(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        self.b(r, "new", "n1", "--title", "N1")
        (self.tmp / "th").mkdir()
        self.b(r, "new", "t", "--title", "T", "--owners", "team", "--home", str(self.tmp / "th"))
        self.b(r, "specialist", "new", "tester", "--title", "Tester")
        self.b(r, "lesson", "tester", "--from", "n1", "check the mobile menu")
        self.assertIn("check the mobile menu", self.b(r, "hire", "tester", "n1", "--task", "test the app"))
        self.assertIn("isn't scoped for team", self.b(r, "hire", "tester", "t", "--task", "x", ok=False))
        self.b(r, "specialist", "new", "qa", "--title", "QA", "--scope", "alice,team")
        self.b(r, "lesson", "qa", "--from", "t", "TEAM-LESSON")
        self.b(r, "lesson", "qa", "--from", "n1", "ALICE-LESSON")
        bt = self.b(r, "hire", "qa", "t", "--task", "review the PR")
        self.assertIn("TEAM-LESSON", bt)
        self.assertNotIn("ALICE-LESSON", bt)
        self.assertIn("Leave nothing in it that names", bt)
        self.assertIn(str(self.tmp / "th"), bt)
        bn = self.b(r, "hire", "qa", "n1", "--task", "x")
        self.assertIn("ALICE-LESSON", bn)
        self.assertNotIn("TEAM-LESSON", bn)
        self.assertIn("✓", self.b(r, "check"))


class Dashboard(Scenario):
    def test_blockers_paused_and_work_in_flight(self):
        r = self.init("alice")
        for nid in ("a", "p"):
            self.b(r, "new", nid, "--title", nid.upper())
        st = r / "nodes" / "a" / "status.md"
        text = st.read_text().replace("## Blocked / needs the operator\n",
                                      "## Blocked / needs the operator\n\n- Pick a hosting plan, cheap or\n"
                                      "  fast WRAPPED-END\n")
        text = text.replace("| --- | --- | --- | --- |\n", "| --- | --- | --- | --- |\n"
                            "| fix-auth | t-1 | #12 | review |\n| old | t-0 | #9 | merged |\n")
        st.write_text(text)
        self.b(r, "relabel", "p", "state:paused")
        out = self.b(r)
        self.assertIn("a: Pick a hosting plan, cheap or fast WRAPPED-END", out)
        self.assertIn("in flight 1", out)
        self.assertIn("[paused]", out)
        self.assertNotIn("p: no coordinator", out)
        self.assertIn("a: no coordinator", out)


class Housekeeping(Scenario):
    def dirty(self, r):
        """Someone else's unfinished work: an edit and a new file in another node."""
        (r / "nodes" / "b" / "status.md").write_text("Updated: x\nHALF-WRITTEN\n")
        (r / "nodes" / "b" / "draft.md").write_text("draft\n")

    def assert_untouched(self, r):
        st = self.git("status", "--porcelain", cwd=r)
        self.assertIn("nodes/b/status.md", st)
        self.assertIn("nodes/b/draft.md", st)
        self.assertNotIn("HALF-WRITTEN", self.git("log", "-p", "--all", "--", "nodes", cwd=r))

    def test_commands_commit_only_what_they_wrote(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "bob")
        for nid in ("a", "b", "c"):
            self.b(r, "new", nid, "--title", nid.upper())
        self.dirty(r)
        self.b(r, "owners", "c", "alice,bob")            # seals c (no home yet)
        self.b(r, "seal", "c", "--owners", "bob", "--home", str(self.tmp / "ch"))
        self.b(r, "move", "a", "c")
        self.b(r, "split", "c/a", "e", "--title", "E")
        self.b(r, "new", "f", "--title", "F")
        self.b(r, "merge", "e", "f")
        (r / "nodes" / "f" / "CLAUDE.md").unlink()
        self.assertIn("fixed: linked nodes/f/CLAUDE.md", self.b(r, "check", "--fix", ok=None))  # c/a is sealed now
        self.b(r, "revoke", "bob")
        self.assert_untouched(r)
        log = self.git("log", "--format=%s", cwd=r)
        for msg in ("c: owners", "c: seal", "Move a", "Split e", "Merge e", "Revoke bob"):
            self.assertIn(msg, log)
        self.assertNotIn("nodes/b/", self.git("log", "--name-only", "--format=", "-8", cwd=r))

    def test_check_fix_repairs_each_kind(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "bob")
        self.b(r, "new", "a", "--title", "A")
        (self.tmp / "vh").mkdir()
        self.b(r, "new", "v", "--title", "V", "--owners", "bob", "--home", str(self.tmp / "vh"))
        self.b(r, "new", "b", "--title", "B")
        a, v = r / "nodes" / "a", r / "nodes" / "v"
        (a / "CLAUDE.md").unlink()                                       # links
        (a / "decisions.md").unlink()                                    # templates
        ag = v / "AGENTS.md"                                             # views
        ag.write_text(ag.read_text().split("## This is the operator's side")[0])
        gi = r / ".gitignore"                                            # gitignore
        gi.write_text(gi.read_text().replace(".brain/\n", ""))
        (r / "registry.md").write_text("stale\n")                        # registry
        self.git("add", "-A", cwd=r); self.git("commit", "-qm", "break things", cwd=r)
        self.dirty(r)
        out = self.b(r, "check", ok=False)
        for msg in ("CLAUDE.md must be a link", "missing decisions.md", "sealed-view section",
                    "doesn't ignore .brain/", "registry.md is out of date"):
            self.assertIn(msg, out)
        out = self.b(r, "check", "--fix")
        self.assertIn("all checks pass", out)
        self.assertTrue((a / "CLAUDE.md").is_symlink())
        self.assertTrue((a / "decisions.md").exists())
        self.assertIn("operator's side of a sealed node", ag.read_text())
        self.assertIn(".brain/", gi.read_text())
        self.assertIn("| v | V | bob | yes", (r / "registry.md").read_text())
        self.assert_untouched(r)


class SealingRules(Scenario):
    def test_seal_writes_the_nodes_rules_into_a_new_home(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "bob")
        self.b(r, "new", "v", "--title", "Venture")
        ag = r / "nodes" / "v" / "AGENTS.md"
        ag.write_text(ag.read_text() + "\n- Prices are in USD, never LBP.\n")
        root_ag = r / "AGENTS.md"
        root_ag.write_text(root_ag.read_text() + "\n- ALICE-PRIVATE: answer in one line.\n")
        self.git("add", "-A", cwd=r); self.git("commit", "-qm", "rules", cwd=r)
        home = self.tmp / "vh"
        out = self.b(r, "seal", "v", "--owners", "alice,bob", "--home", str(home))
        rules = (home / "AGENTS.md").read_text()
        self.assertIn("Prices are in USD, never LBP.", rules)
        self.assertIn("DECISIONS.md", rules)
        self.assertNotIn("ALICE-PRIVATE", rules)                # not copied without consent (axiom 12)
        self.assertIn("ALICE-PRIVATE", out)                     # but offered to the coordinator
        self.assertIn("AGENTS.md", self.git("show", "--name-only", "HEAD", cwd=home))
        self.assertIn("✓", self.b(r, "check"))

    def test_seal_into_an_existing_home_only_proposes(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "bob")
        self.b(r, "new", "v", "--title", "V")
        ag = r / "nodes" / "v" / "AGENTS.md"
        ag.write_text(ag.read_text() + "\n- Ship on Fridays.\n")
        home = self.repo(self.tmp / "vh", {"AGENTS.md": "# Their rules\n"})
        out = self.b(r, "seal", "v", "--owners", "bob", "--home", str(home))
        self.assertEqual((home / "AGENTS.md").read_text(), "# Their rules\n")
        self.assertIn("propose these rules", out)
        self.assertIn("Ship on Fridays.", out)


class Provenance(Scenario):
    def test_citations_survive_moves_and_dangling_ones_are_caught(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        self.b(r, "new", "p", "--title", "P")
        self.b(r, "item", "add", "tone", "--from", "a", "--title", "Tone", "--kind", "style", "--ref", "a/brief.md")
        self.b(r, "specialist", "new", "writer", "--title", "Writer")
        self.b(r, "lesson", "writer", "--from", "a", "shorter titles")
        self.b(r, "move", "a", "p")
        self.assertIn("former_ids: a", (r / "nodes" / "p" / "nodes" / "a" / "node.md").read_text())
        self.assertIn("source_node: a", (r / "library" / "tone.md").read_text())  # never rewritten (axiom 19)
        out = self.b(r, "check")
        self.assertIn("all checks pass", out)
        self.assertNotIn("unknown node", out)
        it = r / "library" / "tone.md"
        it.write_text(it.read_text().replace("source_node: a", "source_node: ghost"))
        self.assertIn("source_node 'ghost'", self.b(r, "check", ok=False))


class Workbench(Scenario):
    def test_failed_setup_keeps_the_worktree_and_reports_failure(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        (r / "nodes" / "app" / "worktree-setup.sh").write_text('echo partial > partial.txt\nexit 7\n')
        p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "wt", "add", "app", "job-fails"],
                           cwd=r, env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 7, p.stdout + p.stderr)
        self.assertIn("setup: FAILED", p.stdout)
        self.assertNotIn("launch a worker there", p.stdout)
        local = json.loads((r / ".brain" / "local.json").read_text())
        path = Path(local["nodes"]["app"]["worktrees"]["job-fails"]["path"])
        self.assertEqual((path / "partial.txt").read_text(), "partial\n")
        self.assertIn("job-fails", self.b(r, "wt", "list", "app"))

    def test_worktrees_with_setup_ports_and_prune(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        origin = self.tmp / "origin.git"
        self.git("init", "-q", "--bare", "-b", "main", str(origin), cwd=self.tmp)
        home = self.repo(self.tmp / "code" / "app", {"README.md": "app\n", ".gitignore": ".env\n"})
        self.git("remote", "add", "origin", str(origin), cwd=home)
        self.git("push", "-q", "-u", "origin", "main", cwd=home)
        self.git("remote", "set-head", "origin", "main", cwd=home)
        (home / ".env").write_text("SECRET=1\n")
        self.b(r, "new", "app", "--title", "App", "--owners", "team", "--home", str(home))
        (r / "nodes" / "app" / "worktree-setup.sh").write_text('cp "$MAIN/.env" .env\necho "PORT=$PORT" >> .env\n')

        out = self.b(r, "wt", "add", "app", "feat-a")
        wa = self.tmp / "code" / "app-work" / "worktrees" / "feat-a"
        self.assertIn("setup: ok", out)
        self.assertIn("SECRET=1", (wa / ".env").read_text())
        self.b(r, "wt", "add", "app", "feat-b")
        wb_ = wa.parent / "feat-b"
        ports = {(w / ".env").read_text().split("PORT=")[1].strip() for w in (wa, wb_)}
        self.assertEqual(len(ports), 2, "each worktree gets its own port")
        self.assertIn("✓", self.b(r, "check"))                  # nothing in the home, nothing in the instance

        # feat-a is done and merged; feat-b has unfinished work; feat-c is new and untouched
        (wa / "a.txt").write_text("a\n")
        self.git("add", "a.txt", cwd=wa); self.git("commit", "-qm", "A", cwd=wa)
        self.git("merge", "-q", "feat-a", cwd=home); self.git("push", "-q", "origin", "main", cwd=home)
        (wb_ / "wip.txt").write_text("wip\n")
        self.b(r, "wt", "add", "app", "feat-c")
        self.assertIn("would remove: feat-a", self.b(r, "wt", "prune", "app", "--dry-run"))
        out = self.b(r, "wt", "prune", "app")
        self.assertIn("removed: feat-a", out)
        self.assertIn("feat-b (uncommitted work)", out)
        self.assertIn("feat-c (not merged)", out)
        self.assertFalse(wa.exists())
        self.assertTrue((wb_ / "wip.txt").exists())
        self.b(r, "wt", "rm", "app", "feat-b", ok=False)        # uncommitted work is never discarded silently
        self.assertIn("feat-c", self.b(r, "wt", "list"))

    def test_reports_have_a_predictable_place(self):
        r = self.init("alice")
        self.b(r, "new", "site", "--title", "Site")
        d = Path(self.b(r, "report", "site", "UI audit: mobile").strip())
        self.assertEqual(d.parent, r / "nodes" / "site" / "reports")
        self.assertTrue(d.name.endswith("-ui-audit-mobile"))
        for h in ("## Answer", "## Findings", "## Controls", "## Not run", "## How to reproduce"):
            self.assertIn(h, (d / "index.md").read_text())
        self.assertIn("UI audit: mobile", self.b(r, "reports"))
        self.assertIn("scripts/", self.b(r, "workbench", "site"))

    def test_a_workbench_inside_the_instance_is_refused(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        home = self.repo(self.tmp / "h")
        self.b(r, "new", "x", "--title", "X", "--owners", "team", "--home", str(home))
        self.b(r, "set", "x", "workbench=" + str(r / "wb"))
        self.assertIn("workbench is inside the instance", self.b(r, "check", ok=False))


class EntryPoints(Scenario):
    def test_global_pointer_is_added_once_and_removable(self):
        r = self.init("alice")
        claude_md = self.tmp / ".claude" / "CLAUDE.md"
        claude_md.parent.mkdir()
        claude_md.write_text("# My own rules\n\nKeep me.\n")
        out = self.b(r, "link", "--global")
        self.assertIn("claude:", out)
        self.assertIn("codex:", out)
        for f in (claude_md, self.tmp / ".codex" / "AGENTS.md"):
            text = f.read_text()
            self.assertIn(f"alice's Brain is at `~/alice`", text)
            self.assertIn("only a pointer", text)
        self.assertIn("already up to date", self.b(r, "link", "--global"))
        self.assertEqual(claude_md.read_text().count("brain:begin"), 1)
        self.assertTrue(claude_md.read_text().startswith("# My own rules\n\nKeep me.\n"))
        self.b(r, "link", "--remove-global")
        self.assertEqual(claude_md.read_text(), "# My own rules\n\nKeep me.\n")
        self.env["CODEX_HOME"] = str(self.tmp / "ch")
        self.b(r, "link", "--global")
        self.assertTrue((self.tmp / "ch" / "AGENTS.md").exists())


class SeveralBrains(Scenario):
    """One machine, two brains (say personal and work): one `brain` command, nothing shared between them."""

    def anywhere(self, *args, ok=True, cwd=None, env=None):
        saved = self.env
        self.env = {**self.env, **(env or {})}
        try:
            return self.run_(list(args), cwd=cwd or self.tmp, ok=ok, brain=CORE / "bin" / "brain")
        finally:
            self.env = saved

    def two(self):
        me = self.init("me")
        self.b(me, "new", "blog", "--title", "My blog")
        work = self.tmp / "Work"
        self.run_(["init", str(work), "--operator", "me", "--title", "Work brain", "--name", "work", "--tz", "UTC"],
                  cwd=self.tmp, brain=CORE / "bin" / "brain")
        self.b(work, "principal", "add", "acme")
        return me, work

    def test_one_command_reaches_each_brain(self):
        me, work = self.two()
        self.assertIn("name: work", (work / "instance.md").read_text())
        out = self.anywhere("brains")
        self.assertIn("me ", out)
        self.assertIn("work ", out)
        self.assertIn("[default]", out.split("\n")[0], "the first brain is the default")
        self.assertIn("root: me brain", self.anywhere("tree"))            # outside both: the default
        self.assertIn("root: Work brain", self.anywhere("-b", "work", "tree"))
        self.assertIn("root: Work brain", self.anywhere("tree", "--brain", "work"))
        self.assertIn("root: me brain", self.anywhere("tree", cwd=me / "nodes"))   # inside one: that one
        self.assertIn("no brain 'nope'", self.anywhere("-b", "nope", "tree", ok=False))
        self.anywhere("use", "work")
        self.assertIn("root: Work brain", self.anywhere("tree"))
        self.assertIn("root: me brain", self.anywhere("tree", cwd=me))
        out = self.anywhere("--all")
        self.assertIn("===== me =====", out)
        self.assertIn("===== work =====", out)
        self.assertIn("Work brain (work)", out)

    def test_a_project_folder_belongs_to_its_brain(self):
        me, work = self.two()
        repo = self.repo(self.tmp / "code" / "acme-app")
        self.b(work, "new", "app", "--title", "Acme app", "--owners", "acme", "--home", str(repo))
        self.assertIn("root: Work brain", self.anywhere("tree", cwd=repo))
        self.b(work, "wt", "add", "app", "fix-1")
        wt = self.tmp / "code" / "acme-app-work" / "worktrees" / "fix-1"
        self.assertIn("root: Work brain", self.anywhere("tree", cwd=wt))
        self.assertIn("root: me brain", self.anywhere("tree", cwd=self.tmp / "code"))

    def test_each_brain_runs_its_own_core(self):
        me, work = self.two()
        f = work / "core" / "bin" / "brain"
        f.write_text(f.read_text().replace("def main():\n", "def main():\n    print('WORK CORE')\n", 1))
        env = {k: v for k, v in self.env.items() if k != "BRAIN_NO_REEXEC"}
        self.assertIn("WORK CORE", self.anywhere("-b", "work", "now", env={**env, "BRAIN_NO_REEXEC": ""}))
        self.assertNotIn("WORK CORE", self.anywhere("now", env={**env, "BRAIN_NO_REEXEC": ""}))

    def test_pointers_go_where_each_brain_is_told(self):
        me, work = self.two()
        claude, codex = self.tmp / ".claude" / "CLAUDE.md", self.tmp / ".codex" / "AGENTS.md"
        self.b(me, "link", "--global")
        self.assertIn("`brain -b me`", claude.read_text())
        out = self.b(work, "link", "--global")                      # the default folders already point to 'me'
        self.assertIn("already points to me: skipped", out)
        self.assertNotIn("Work brain", claude.read_text())
        self.b(work, "link", "--global", "claude=~/.claude-work", "codex=~/.codex-work")
        cw = self.tmp / ".claude-work" / "CLAUDE.md"
        self.assertIn("`brain -b work`", cw.read_text())
        self.assertIn("`brain -b work`", (self.tmp / ".codex-work" / "AGENTS.md").read_text())
        self.assertNotIn("me brain", cw.read_text())
        self.assertNotIn("Work brain", claude.read_text() + codex.read_text())
        self.assertIn("pointer in: claude ~/.claude-work; codex ~/.codex-work", self.anywhere("brains"))
        # a session started with the work config folder reaches the work brain, wherever it runs
        self.assertIn("root: Work brain", self.anywhere("tree", env={"CLAUDE_CONFIG_DIR": str(cw.parent)}))
        self.assertIn("root: me brain", self.anywhere("tree"))
        # moving it: the pointer leaves the old folder
        self.b(work, "link", "--global", "claude=~/.claude-w2")
        self.assertNotIn("brain:begin work", cw.read_text())
        self.assertIn("brain:begin work", (self.tmp / ".claude-w2" / "CLAUDE.md").read_text())
        # sharing a folder is allowed when asked for, and each pointer comes out on its own
        self.b(work, "link", "--global", "claude=~/.claude")
        self.assertEqual(claude.read_text().count("brain:begin"), 2)
        self.b(work, "link", "--remove-global")
        self.assertIn("brain:begin me", claude.read_text())
        self.assertNotIn("brain:begin work", claude.read_text())

    def test_an_older_pointer_is_upgraded_in_place(self):
        me = self.init("me")
        claude = self.tmp / ".claude" / "CLAUDE.md"
        claude.parent.mkdir()
        claude.write_text("# Mine\n\n<!-- brain:begin (written by `brain link --global`; edit outside these lines) -->\n"
                          "me's Brain is at `~/me`: old text\n<!-- brain:end -->\n")
        self.b(me, "link")
        text = claude.read_text()
        self.assertTrue(text.startswith("# Mine\n"))
        self.assertNotIn("old text", text)
        self.assertEqual(text.count("brain:begin"), 1)
        self.assertIn("brain:begin me", text)
        self.assertEqual(json.loads((me / ".brain" / "local.json").read_text())["global"], {"claude": ["~/.claude"]})
        self.assertIn("this brain: me   pointer in: claude ~/.claude", self.b(me, "machine"))

    def test_a_throwaway_brain_stays_out_of_the_real_list(self):
        import tempfile as tf
        real = self.tmp / "real-config"                       # stands in for ~/.config/brain, outside the temp dir?
        if str(real.resolve()).startswith(str(Path(tf.gettempdir()).resolve())):
            # this test's folders are themselves in the temp dir; point the list somewhere that isn't
            real = Path.home() / ".cache" / f"brain-test-{os.getpid()}"
        self.env["BRAIN_CONFIG"] = str(real / "brains.json")
        try:
            r = self.init("alice")                             # lives in the temp dir
            self.b(r, "tree")
            self.assertFalse((real / "brains.json").exists() and "alice" in (real / "brains.json").read_text())
        finally:
            shutil.rmtree(real, ignore_errors=True)

    def test_ports_never_collide_across_brains(self):
        me, work = self.two()
        self.b(me, "principal", "add", "club")
        a = self.repo(self.tmp / "a")
        w = self.repo(self.tmp / "w")
        self.b(me, "new", "a", "--title", "A", "--owners", "club", "--home", str(a))
        self.b(work, "new", "w", "--title", "W", "--owners", "acme", "--home", str(w))
        self.b(me, "set", "a", "ports=3")
        (me / "nodes" / "a" / "worktree-setup.sh").write_text('echo "$PORT $PORTS $PORT_LAST" > ports.txt\n')
        self.assertIn("ports ", self.b(me, "wt", "add", "a", "j1"))
        p, n, last = map(int, (self.tmp / "a-work" / "worktrees" / "j1" / "ports.txt").read_text().split())
        self.assertEqual((n, last), (3, p + 2))
        self.b(work, "wt", "add", "w", "j2")
        q = json.loads((work / ".brain" / "local.json").read_text())["nodes"]["w"]["worktrees"]["j2"]["port"]
        self.assertNotIn(q, range(p, p + 3), "another brain's worktree never gets these ports")
        self.assertIn(f"ports {p}-{p + 2}", self.b(me, "wt", "list"))
        self.b(me, "set", "a", "ports=lots")
        self.assertIn("ports must be a whole number", self.b(me, "wt", "add", "a", "j3", ok=False))



class WorkerBriefs(Scenario):
    def test_a_brief_carries_the_job_its_limits_and_the_hand_back(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, "set", "app", "coordinator=thr-app", "ports=3")
        brief = r / "nodes" / "app" / "brief.md"
        text = brief.read_text()
        self.assertIn("## Limits", text)                              # the template has the section
        brief.write_text(text.replace("## Sources of truth", (
            "- At most 2 worktrees at once. Why: the disk is nearly full. Lift when: over 50 GB free.\n"
            "- No deploys.\n\n## Sources of truth")))
        self.b(r, "wt", "add", "app", "job-a")
        out = self.b(r, "brief", "app", "--branch", "job-a", "--task", "Fix the login bug.")
        self.assertIn("Run in: ~/code/app-work/worktrees/job-a", out)
        self.assertIn("ports ", out)                                 # the job's block of ports
        self.assertIn("Fix the login bug.", out)
        self.assertIn("Lift when: over 50 GB free", out)              # limits travel with their reasons
        self.assertIn("Never ask the operator", out)
        self.assertIn("thr-app", out)                                 # who to wake, and how
        self.assertIn("t3_thread_send", out)
        self.assertIn("~/alice/nodes/app/inbox.md", out)
        self.assertIn("Lessons", out)
        self.b(r, "brief", "app", "--branch", "nope", ok=False)
        # the same hand-back in a specialist's brief and a review brief
        self.b(r, "specialist", "new", "qa", "--title", "QA")
        hb = self.b(r, "hire", "qa", "app", "--task", "test it")
        self.assertIn("Never ask the operator", hb)
        self.assertIn("Lift when: over 50 GB free", hb)
        rb = self.b(r, "review", "app", "--branch", "job-a")
        self.assertIn("thr-app", rb)
        self.assertIn("Lift when: over 50 GB free", rb)
        self.assertNotIn("inbox.md", rb)                              # a reviewer changes nothing, not even that
        self.assertIn("Write no file", rb)
        # a limit with no reason or end is flagged; show gives the status time in UTC for host times
        self.assertIn("a limit in brief.md has no Why: or Lift when:", self.b(r, "check"))
        self.assertRegex(self.b(r, "show", "app"), r"\(= \d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z")
        # a sealed node's brief names nothing above it
        self.b(r, "principal", "add", "team")
        sh = self.repo(self.tmp / "code" / "shared")
        self.b(r, "new", "shared", "--title", "Shared", "--owners", "team", "--home", str(sh))
        sb = self.b(r, "brief", "shared", "--task", "x")
        self.assertNotIn("alice", sb)
        loc = json.loads((r / ".brain" / "local.json").read_text())   # this machine loses the home's path
        home_was = loc["nodes"]["shared"].pop("home")
        (r / ".brain" / "local.json").write_text(json.dumps(loc))
        out = self.b(r, "brief", "shared", "--task", "x", ok=False)
        self.assertIn("isn't on this machine", out)
        loc["nodes"]["shared"]["home"] = home_was
        (r / ".brain" / "local.json").write_text(json.dumps(loc))
        self.assertIn("Leave nothing in it that names", sb)
        d = Path(self.b(r, "report", "app", "job a").strip())
        self.assertIn("## Lessons", (d / "index.md").read_text())

    def test_words_a_home_must_never_contain(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app", {"README.md": "app\n", ".gitignore": "backups/\n"})
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        (r / "nodes" / "app" / "never-name.txt").write_text("# earlier codebases\nOldCo\n")
        self.assertIn("guard: clean", self.b(r, "guard", "app"))
        (home / "backups").mkdir()
        (home / "backups" / "dump.bin").write_text("xx oldco xx\n")       # ignored files don't count
        self.assertIn("guard: clean", self.b(r, "guard", "app"))
        (home / "notes.md").write_text("ported from OLDCO\n")               # untracked, any case
        out = self.b(r, "guard", "app", ok=False)
        self.assertIn("notes.md: term 1 of app/never-name.txt", out)
        self.assertNotIn("oldco", out.lower())                               # never shows the word
        self.assertIn("contains term 1 of app/never-name.txt", self.b(r, "check", ok=False))
        (home / "notes.md").unlink()
        out = self.b(r, "wt", "add", "app", "port-oldco", ok=False)        # brain won't write the word itself
        self.assertNotIn("oldco", out.lower())
        self.assertFalse((r / "nodes" / "app" / "job-ledger.md").exists())
        self.git("branch", "port-OldCo", cwd=home)                           # a branch name someone else made
        self.b(r, "wt", "add", "app", "port")
        w = self.tmp / "code" / "app-work" / "worktrees" / "port"
        (w / "x.md").write_text("x\n")
        self.git("add", "x.md", cwd=w)
        self.git("commit", "-qm", "like oldco did", cwd=w)                   # a commit message
        (w / "oldco-notes.txt").write_text("nothing here\n")                 # a file name
        out = self.b(r, "guard", "app", ok=False)
        self.assertIn("branch port-[…]", out)
        self.assertIn("[…]-notes.txt", out)
        self.assertRegex(out, r"commit [0-9a-f]+ in")
        self.assertNotIn("oldco", out.lower())
        self.assertNotIn("oldco", self.b(r, "check", ok=False).lower())
        self.assertNotIn("oldco", self.b(r, "ui", "--json").lower())       # the live view shows it as guard does
        out = self.b(r, "guard", "app", str(self.tmp / "nowhere"), ok=False)  # a path that isn't there isn't clean
        self.assertIn("not found", out)
        (w / "oldco-notes.txt").unlink()
        # a child's home is covered by the parent's words; briefs that carry a word are refused
        kid = self.repo(self.tmp / "code" / "kid", {"a.md": "OldCo inside\n"})
        self.b(r, "new", "app/kid", "--title", "Kid", "--home", str(kid))
        self.assertIn("a.md: term 1 of app/never-name.txt", self.b(r, "guard", "app/kid", ok=False))
        self.assertIn("guard", self.b(r, "brief", "app", "--task", "tidy up"))
        out = self.b(r, "brief", "app", "--task", "remove what came from OldCo", ok=False)
        self.assertNotIn("oldco", out.lower())
        self.b(r, "new", "plain", "--title", "Plain")
        self.assertIn("nothing to check", self.b(r, "guard", "plain", str(kid)))
        # a node whose home is its own folder: its files are checked, its notes (which state the rule) aren't
        self.b(r, "new", "app/notes", "--title", "Notes")
        nd = next(p_.parent for p_ in r.rglob("node.md") if "id: app/notes" in p_.read_text())
        (nd / "brief.md").write_text((nd / "brief.md").read_text() + "\nNever name OldCo.\n")
        self.assertIn("guard: clean", self.b(r, "guard", "app/notes"))
        (nd / "sources" / "draft.md").write_text("from OldCo\n")
        self.assertIn("draft.md: term 1 of app/never-name.txt", self.b(r, "guard", "app/notes", ok=False))




class Hardening(Scenario):
    """What the second-runtime reviews broke, kept broken-proof."""

    def never(self, r, node):
        nd = next(p_.parent for p_ in r.rglob("node.md") if f"id: {node}\n" in p_.read_text())
        (nd / "never-name.txt").write_text("OldCo\n")
        return nd

    def test_guard_on_a_home_inside_the_brain(self):
        r = self.init("alice")
        self.b(r, "new", "notes", "--title", "Notes")
        nd = self.never(r, "notes")
        (r / ".gitignore").write_text((r / ".gitignore").read_text() + "dist/\n")
        (nd / "dist").mkdir()
        (nd / "dist" / "gen.js").write_text("OldCo\n")                    # ignored: not part of the home
        os.mkfifo(nd / "sources" / "events.pipe")                         # a special file never blocks the scan
        self.assertIn("guard: clean", self.b(r, "guard", "notes"))
        (nd / ".github").mkdir()
        (nd / ".github" / "ci.yml").write_text("run: oldco\n")             # dot-folders are part of it
        (nd / "sources" / "OldCo").mkdir()
        (nd / "sources" / "OldCo" / "clean.txt").write_text("nothing\n")   # a folder's name is too
        out = self.b(r, "guard", "notes", ok=False)
        self.assertIn(".github/ci.yml", out)
        self.assertIn("sources/[…]/clean.txt", out)
        self.assertNotIn("oldco", out.lower())

    def test_an_unreadable_repository_is_never_clean(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.never(r, "app")
        (home / ".git" / "HEAD").rename(home / ".git" / "HEAD.bak")
        r_ = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "guard", "app"], cwd=r, env=self.env,
                            capture_output=True, text=True)
        self.assertEqual(r_.returncode, 2, r_.stdout + r_.stderr)
        self.assertIn("incomplete", r_.stdout)
        (home / ".git" / "HEAD.bak").rename(home / ".git" / "HEAD")
        out = self.b(r, "guard", "app", str(self.tmp / "OldCo-missing"), ok=False)
        self.assertNotIn("oldco", out.lower())

    def test_no_review_brief_into_a_missing_home(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, "review", "app", "--branch", "main", "--base", "main")
        loc = json.loads((r / ".brain" / "local.json").read_text())
        loc["nodes"]["app"].pop("home")
        (r / ".brain" / "local.json").write_text(json.dumps(loc))
        self.assertIn("isn't on this machine", self.b(r, "review", "app", "--branch", "main", ok=False))

    def test_a_git_hook_that_runs_brain_does_not_deadlock(self):
        r = self.init("alice")
        gi = r / ".gitignore"                       # something for the hook's `check --fix` to repair and commit
        gi.write_text(gi.read_text().replace(".brain/\n", ""))
        self.git("commit", "-qam", "drop .brain/ from .gitignore", cwd=r)
        hook = r / ".git" / "hooks" / "post-commit"
        hook.write_text(f"#!/bin/sh\n'{sys.executable}' '{r / 'core' / 'bin' / 'brain'}' check --fix >/dev/null 2>&1\n")
        hook.chmod(0o755)
        p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "new", "x", "--title", "X"], cwd=r,
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(".brain/", gi.read_text())    # the hook did run, inside brain's own commit

    def test_a_branch_called_job_keeps_its_row(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, "wt", "add", "app", "Job")
        self.b(r, "review", "app", "--branch", "Job", "--built-by", "claude")
        self.b(r, "wt", "add", "app", "other")
        self.assertIn("open  Job", self.b(r, "jobs", "app"))
        self.assertIn("| Job | 20", (r / "nodes" / "app" / "job-ledger.md").read_text())


class Hardening3(Scenario):
    """Round three of the second-runtime review."""

    def test_commands_that_change_things_take_turns(self):
        r = self.init("alice")
        for i in range(6):
            self.b(r, "new", f"n{i}", "--title", f"N{i}")
        ps = [subprocess.Popen([sys.executable, str(r / "core" / "bin" / "brain"), "set", f"n{i}", f"coordinator=t{i}"],
                               cwd=r, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
              for i in range(6)]
        self.assertEqual([p.communicate()[1] and p.returncode or p.returncode for p in ps], [0] * 6)
        loc = json.loads((r / ".brain" / "local.json").read_text())
        self.assertEqual({k: v.get("coordinator") for k, v in loc["nodes"].items() if k.startswith("n")},
                         {f"n{i}": f"t{i}" for i in range(6)})            # no binding lost

    def test_a_dead_holders_permission_does_not_outlive_it(self):
        import fcntl
        r = self.init("alice")
        lock = r / ".brain" / "lock"
        lock.parent.mkdir(exist_ok=True)
        with open(lock, "a+") as fh:                                  # someone else holds the lock now
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.seek(0); fh.truncate(); fh.write("1"); fh.flush()     # pid 1: alive, but not our ancestor's lock...
            env = {**self.env, "BRAIN_LOCK_HELD": f"{r.resolve()}|999999", "BRAIN_LOCK_WAIT": "2"}  # ...a dead holder
            p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "new", "x", "--title", "X"], cwd=r,
                               env=env, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(p.returncode, 0)                      # it waited, then gave up: it didn't skip
            self.assertIn("held this instance's lock", p.stderr)
        self.assertNotIn("id: x\n", "".join(q.read_text() for q in r.rglob("node.md")))

    def test_guard_edge_cases(self):
        r = self.init("alice")
        self.b(r, "new", "notes", "--title", "Notes")
        nd = r / "nodes" / "notes"
        (nd / "never-name.txt").write_text("OldCo\n")
        comp = nd / "sources" / "component"
        comp.mkdir(parents=True)
        (comp / "node.md").write_text("# docs for a component, not a brain node\n")
        (comp / "artifact.py").write_text("# from OldCo\n")
        self.assertIn("artifact.py", self.b(r, "guard", "notes", ok=False))   # only real child nodes are skipped
        (comp / "artifact.py").write_text("clean\n")
        locked = nd / "sources" / "locked.txt"
        locked.write_text("OldCo\n")
        locked.chmod(0)
        try:
            p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "guard", "notes"], cwd=r,
                               env=self.env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, p.stdout)                       # unreadable isn't clean
            self.assertIn("couldn't read", p.stdout)
        finally:
            locked.chmod(0o644)
        # a sealed home whose repository can't be read: incomplete, not clean
        self.b(r, "principal", "add", "team")
        sh = self.repo(self.tmp / "code" / "shared")
        self.b(r, "new", "shared", "--title", "Shared", "--owners", "team", "--home", str(sh))
        (sh / ".git" / "HEAD").rename(sh / ".git" / "HEAD.bak")
        p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "guard", "shared"], cwd=r,
                           env=self.env, capture_output=True, text=True)
        (sh / ".git" / "HEAD.bak").rename(sh / ".git" / "HEAD")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)

    def test_decisions_with_offsets_reach_the_live_view(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        d = r / "nodes" / "a" / "decisions.md"
        d.write_text(d.read_text() + "\n## 2026-10-19 06:32 +03:00: Use Postgres\nWhy: writes.\n"
                     "\n## 2026-01-10 09:00: An old one, in winter\nWhy: x.\n")
        st = json.loads(self.b(r, "ui", "--json"))
        dec = {x["title"]: x["t"] for x in next(n for n in st["nodes"] if n["id"] == "a")["decisions"]}
        self.assertEqual(dec["Use Postgres"], 1792380720)                  # 03:32Z
        self.assertEqual(dec["An old one, in winter"], 1768035600)         # UTC instance: 09:00Z


class Hardening4(Scenario):
    """Round four of the second-runtime review."""

    def held(self, r):
        import fcntl
        lock = r / ".brain" / "lock"
        lock.parent.mkdir(exist_ok=True)
        fh = open(lock, "a+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fh

    def run_b(self, r, *args, env=None):
        return subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), *args], cwd=r,
                              env={**self.env, "BRAIN_LOCK_WAIT": "2", **(env or {})}, capture_output=True, text=True,
                              input="", timeout=60)

    def test_unreadable_files_in_a_repository_are_not_clean(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app", {"a.txt": "OldCo\n"})
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        (r / "nodes" / "app" / "never-name.txt").write_text("OldCo\n")
        (home / "a.txt").chmod(0)
        try:
            p = self.run_b(r, "guard", "app")
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        finally:
            (home / "a.txt").chmod(0o644)

    def test_every_writer_waits_and_readers_do_not(self):
        r = self.init("alice")
        self.b(r, "item", "add", "m1", "--from", "root", "--title", "M", "--kind", "method", "--ref", "x", "--body", "b")
        item = next(r.rglob("library/m1.md"))
        before = item.read_text()
        fh = self.held(r)
        try:
            p = self.run_b(r, "item", "quarantine", "m1")
            self.assertNotEqual(p.returncode, 0)                     # it waited, then gave up...
            self.assertEqual(item.read_text(), before)               # ...without touching the item
            out = r / "nodes" / "brief.txt"
            self.assertNotEqual(self.run_b(r, "brief", "root", "--task", "x", "--out", str(out)).returncode, 0)
            for args in (["principal", "list"], ["schedule", "list"], ["brief", "root", "--task", "x"], ["show", "root"]):
                self.assertEqual(self.run_b(r, *args).returncode, 0, args)   # reading never waits
        finally:
            fh.close()

    def test_the_stop_hook_never_breaks_a_turn(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        a = r / "nodes" / "a"
        st = a / "status.md"
        st.write_text(st.read_text().replace(st.read_text().splitlines()[0], "Updated: 2020-01-01 00:00 by a coordinator"))
        (r / ".brain").mkdir(exist_ok=True)
        (r / ".brain" / "hooks.json").write_text("{half a fi")              # half-written by another session
        payload = json.dumps({"cwd": str(a), "session_id": "s1"})
        p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "hook", "stop"], cwd=a, env=self.env,
                           input=payload, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('"decision": "block"', p.stdout)                      # the reminder still goes out
        (r / ".brain").chmod(0o500)                                         # nothing can be written there
        try:
            p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "hook", "stop"], cwd=a, env=self.env,
                               input=json.dumps({"cwd": str(a), "session_id": "s2"}), capture_output=True, text=True,
                               timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stderr, "")
        finally:
            (r / ".brain").chmod(0o755)


class Hardening5(Scenario):
    """Round five of the second-runtime review."""

    def test_big_files_and_unreadable_folders(self):
        r = self.init("alice")
        self.b(r, "new", "notes", "--title", "Notes")
        nd = r / "nodes" / "notes"
        (nd / "never-name.txt").write_text("OldCo\n")
        big = nd / "sources" / "big.txt"
        big.write_bytes(b"x" * (2_000_010 - 3) + b"Old" + b"Co and more\n")   # past 2 MB, across a 1 MiB boundary
        self.assertIn("big.txt", self.b(r, "guard", "notes", ok=False))
        big.write_bytes(b"x" * ((1 << 20) - 3) + b"OldCo\n")                     # split exactly at a chunk edge
        self.assertIn("big.txt", self.b(r, "guard", "notes", ok=False))
        big.unlink()
        hidden = nd / "sources" / "hidden"
        hidden.mkdir()
        (hidden / "f.txt").write_text("OldCo\n")
        hidden.chmod(0)
        try:
            p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "guard", "notes"], cwd=r,
                               env=self.env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, p.stdout)                           # couldn't look: not clean
        finally:
            hidden.chmod(0o755)


class Hardening6(Scenario):
    """Round six of the second-runtime review."""

    def test_a_character_split_between_reads_is_still_found(self):
        r = self.init("alice")
        self.b(r, "new", "notes", "--title", "Notes")
        nd = r / "nodes" / "notes"
        (nd / "never-name.txt").write_text("Café\n")
        f = nd / "sources" / "edge.txt"
        for pad in range(1, 6):                                       # é straddles the 1 MiB edge at some pad
            f.write_bytes(b"x" * ((1 << 20) - pad) + "Café\n".encode())
            self.assertIn("edge.txt", self.b(r, "guard", "notes", ok=False), pad)

    def test_seal_commits_nothing_that_names_a_never_word(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        self.b(r, "new", "legacy", "--title", "OldCo port")
        (r / "nodes" / "legacy" / "never-name.txt").write_text("OldCo\n")
        home = self.tmp / "code" / "legacy"
        p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "seal", "legacy", "--owners", "team",
                            "--home", str(home)], cwd=r, env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("oldco", (p.stdout + p.stderr).lower())
        self.assertEqual(subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=home,
                                        capture_output=True).returncode, 1 if (home / ".git").exists() else 128)


class Hardening7(Scenario):
    """Round seven of the second-runtime review."""

    def test_binary_files_greek_words_and_the_word_list_itself(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app", {".gitattributes": "*.dat binary\n"})
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        (r / "nodes" / "app" / "never-name.txt").write_text("OldCo\nΟΣ\n")
        (home / "x.dat").write_bytes(b"\0\1OldCo\0")                       # git calls it binary
        self.assertIn("x.dat", self.b(r, "guard", "app", ok=False))
        (home / "x.dat").unlink()
        for pad in (2, 3):                                                   # Σ on a chunk edge
            (home / "g.txt").write_bytes(b"x" * ((1 << 20) - pad) + "ΟΣ\n".encode())
            self.assertIn("g.txt", self.b(r, "guard", "app", ok=False))
        (home / "g.txt").unlink()
        self.b(r, "principal", "add", "team")
        self.b(r, "new", "legacy", "--title", "Legacy")
        (r / "nodes" / "legacy" / "never-name.txt").write_text("OldCo\n")
        for rel in ("never-name.txt", "./never-name.txt", "sources/../never-name.txt"):
            out = self.b(r, "seal", "legacy", "--owners", "team", "--home", str(self.tmp / "h"), "--move", rel, ok=False)
            self.assertIn("stays on the operator's side", out)
            self.assertTrue((r / "nodes" / "legacy" / "never-name.txt").exists())
            self.assertFalse((self.tmp / "h").exists())                       # refused before anything happened

class Hardening8(Scenario):
    """Ordinary staged edits, deletions and populated submodules from review round eight."""

    def test_runtime_links_visit_only_registered_nodes(self):
        r = self.init("alice")
        impostors = [r / ".git" / "nodes" / "fake", r / "artifacts" / "nodes" / "fake"]
        for folder in impostors:
            folder.mkdir(parents=True)
            (folder / "AGENTS.md").write_text("Unrelated instructions\n")
        self.b(r, "new", "a", "--title", "A")
        self.b(r, "new", "a/b", "--title", "B")
        for folder in impostors:
            self.assertFalse((folder / "CLAUDE.md").exists())
        self.assertTrue((r / "nodes" / "a" / "nodes" / "b" / "CLAUDE.md").is_symlink())

    def test_index_contents_and_tracked_deletions(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app", {"a.txt": "clean\n"})
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        (r / "nodes" / "app" / "never-name.txt").write_text("OldCo\nCafé\n")
        f = home / "a.txt"
        # Binary content and an accented term split across a chunk edge must also be checked in the index.
        f.write_bytes(b"\0OldCo\n" + b"x" * ((1 << 20) - 9) + "Café\n".encode())
        self.git("add", "a.txt", cwd=home)
        f.write_text("clean again\n")
        out = self.b(r, "guard", "app", ok=False)
        self.assertIn("index stage 0:", out)
        self.assertIn("term 1", out)
        self.assertIn("term 2", out)
        self.assertNotIn("OldCo", out)
        f.unlink()
        out = self.b(r, "guard", "app", ok=False)
        self.assertIn("index stage 0:", out)
        self.assertNotIn("incomplete", out)
        self.git("add", "-u", cwd=home)
        self.assertIn("clean", self.b(r, "guard", "app"))
        # A clean unstaged deletion is clean too, while its clean blob remains staged.
        self.git("restore", "--source=HEAD", "--staged", "--worktree", "a.txt", cwd=home)
        f.unlink()
        self.assertIn("clean", self.b(r, "guard", "app"))

    def test_submodules_include_working_index_and_history(self):
        r = self.init("alice")
        lib = self.repo(self.tmp / "lib", {"note.txt": "clean\n"})
        home = self.repo(self.tmp / "code" / "app")
        self.git("submodule", "add", "-q", str(lib), "vendor/lib", cwd=home)
        self.git("commit", "-qam", "add library", cwd=home)
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        (r / "nodes" / "app" / "never-name.txt").write_text("OldCo\n")
        sub = home / "vendor" / "lib"
        self.assertIn("clean", self.b(r, "guard", "app"))
        (sub / "note.txt").write_text("OldCo\n")
        self.assertIn("vendor/lib/note.txt", self.b(r, "guard", "app", ok=False))
        self.git("add", "note.txt", cwd=sub)
        (sub / "note.txt").write_text("clean\n")
        self.assertIn("index stage 0:", self.b(r, "guard", "app", ok=False))
        self.git("restore", "--staged", "note.txt", cwd=sub)
        self.git("branch", "OldCo-migration", cwd=sub)
        self.assertIn("branch", self.b(r, "guard", "app", ok=False))
        self.git("branch", "-D", "OldCo-migration", cwd=sub)
        self.git("commit", "--allow-empty", "-qm", "OldCo migration", cwd=sub)
        self.assertIn("commit", self.b(r, "guard", "app", ok=False))
        self.git("submodule", "deinit", "-f", "vendor/lib", cwd=home)
        out = self.b(r, "guard", "app", ok=False)
        self.assertIn("incomplete", out)
        self.assertIn("submodule unavailable", out)

    def test_seal_redacts_inherited_owner_summary(self):
        r = self.init("oldco")
        self.b(r, "new", "legacy", "--title", "Legacy")
        (r / "nodes" / "legacy" / "never-name.txt").write_text("OldCo\n")
        self.b(r, "principal", "add", "team")
        out = self.b(r, "seal", "legacy", "--owners", "team", "--home", str(self.tmp / "code" / "legacy"))
        self.assertIn("effective owners", out)
        self.assertNotIn("oldco", out.casefold())


class FutureTimestamps(Scenario):
    def test_five_minute_skew_uses_elapsed_time_across_dst(self):
        import datetime as dt
        from importlib.machinery import SourceFileLoader
        from unittest import mock
        from zoneinfo import ZoneInfo
        m = SourceFileLoader("brain_future", str(CORE / "bin/brain")).load_module()
        r = self.init("alice")
        inst = m.Instance(r)
        node = inst.node("root")
        now = dt.datetime(2026, 11, 1, 1, 59, tzinfo=ZoneInfo("America/New_York"))
        with mock.patch.object(inst, "now_dt", return_value=now):
            (r / "status.md").write_text("Updated: 2026-11-01 01:03 -05:00\n")  # four actual minutes later
            self.assertEqual(m.future_entries(inst, node)[0], [])
            (r / "status.md").write_text("Updated: 2026-11-01 01:10 -05:00\n")  # eleven actual minutes later
            self.assertIn("future Updated:", m.future_entries(inst, node)[0][0])

    def test_offsets_skew_and_legacy_entries(self):
        import datetime as dt
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        cfg = r / "instance.md"
        cfg.write_text(cfg.read_text().replace("timezone: UTC", "timezone: Asia/Beirut"))
        st = r / "nodes/a/status.md"
        from zoneinfo import ZoneInfo
        now = dt.datetime.now(ZoneInfo("Asia/Beirut"))
        for when in (now + dt.timedelta(minutes=4), now - dt.timedelta(days=1)):
            st.write_text(f"Updated: {when:%Y-%m-%d %H:%M} by coordinator\n")
            self.assertNotIn("future", self.b(r, "check"))
        future = now + dt.timedelta(minutes=20)
        for text in (future.strftime("%Y-%m-%d %H:%M"), future.isoformat(timespec="seconds"),
                     future.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M +00:00")):
            st.write_text(f"Updated: {text} by coordinator\n")
            self.assertIn("future Updated: time", self.b(r, "check", ok=False))
        st.write_text("Updated: 15:58 by coordinator\n- 23:59: old log\n")
        self.assertNotIn("future", self.b(r, "check"))

    def test_other_dated_entries_warn_and_hook_explains_fix(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        node = r / "nodes/a"
        (node / "decisions.md").write_text("## 2999-01-01 00:00 +03:00: decision\n")
        st = node / "status.md"
        st.write_text("Updated: 2020-01-01 00:00\n- 2999-01-02: log entry\n- 23:59: old\n")
        out = self.b(r, "check")
        self.assertIn("! a: decisions.md:1: future dated entry", out)
        self.assertIn("! a: status.md:2: future dated entry", out)
        st.write_text("Updated: 2999-01-01 00:00 +03:00 by coordinator\n")
        p = subprocess.run([sys.executable, str(CORE / "bin/brain"), "hook", "stop"], cwd=node,
                           input=json.dumps({"cwd": str(node)}), capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0)
        self.assertIn("future Updated: time; fix it with `brain now`", json.loads(p.stdout)["reason"])


class FutureEntryRegressions(Scenario):
    def test_invalid_utf8_and_nonfiles_do_not_break_views_or_stop_hook(self):
        r = self.init("alice")
        (r / "notes.md").write_bytes(b"invalid \xff\n- 2999-01-01: future note\n")
        (r / "directory.md").mkdir()
        if hasattr(os, "mkfifo"):
            os.mkfifo(r / "pipe.md")
        self.assertIn("notes.md:2: future dated entry", self.b(r, "check"))
        self.assertIn("alice brain", self.b(r))
        self.assertIn('"nodes"', self.b(r, "ui", "--json"))
        st = r / "status.md"
        st.write_text("Updated: 2999-01-01 00:00 +00:00\n")
        res = subprocess.run([sys.executable, str(CORE / "bin/brain"), "hook", "stop"], cwd=r,
                             input=json.dumps({"cwd": str(r), "session_id": "invalid-utf8"}),
                             capture_output=True, text=True, env=self.env)
        self.assertEqual(res.returncode, 0)
        self.assertIn("future Updated:", json.loads(res.stdout)["reason"])

    def test_unreadable_markdown_is_skipped(self):
        from importlib.machinery import SourceFileLoader
        from unittest import mock
        m = SourceFileLoader("brain_unreadable_future", str(CORE / "bin/brain")).load_module()
        r = self.init("alice")
        bad = r / "unreadable.md"
        bad.write_text("not readable\n")
        (r / "readable.md").write_text("- 2999-01-01: future\n")
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == bad:
                raise PermissionError("unreadable")
            return original(path, *args, **kwargs)
        inst = m.Instance(r)
        with mock.patch.object(Path, "read_text", read):
            problems, warnings = m.future_entries(inst, inst.node("root"))
        self.assertEqual(problems, [])
        self.assertTrue(any("readable.md:1" in w for w in warnings))

    def test_human_time_ranges_are_not_negative_offsets(self):
        import datetime as dt
        from importlib.machinery import SourceFileLoader
        from unittest import mock
        m = SourceFileLoader("brain_range_future", str(CORE / "bin/brain")).load_module()
        r = self.init("alice")
        inst = m.Instance(r)
        node = inst.node("root")
        now = dt.datetime(2026, 10, 19, 12, tzinfo=dt.timezone.utc)
        with mock.patch.object(inst, "now_dt", return_value=now):
            for text in ("2026-10-19 09:00-10:30", "2026-10-19 09:00-1030",
                         "2026-10-19 09:00:00-10:30"):
                (r / "status.md").write_text(f"Updated: {text}\n- {text}: meeting\n")
                problems, warnings = m.future_entries(inst, node)
                self.assertEqual(problems, [], text)
                self.assertFalse(any("status.md:" in w for w in warnings), text)
            # Explicit spaced offsets and ISO T timestamps still compare actual moments.
            for text in ("2026-10-19 09:00 -10:30", "2026-10-19T09:00-10:30",
                         "2026-10-19T09:00:00.123-10:30"):
                (r / "status.md").write_text(f"Updated: {text}\n")
                self.assertEqual(len(m.future_entries(inst, node)[0]), 1, text)

    def test_future_reminders_are_spaced_per_session_and_address_workers(self):
        r = self.init("alice")
        (r / "status.md").write_text("Updated: 2999-01-01 00:00 +00:00\n")
        self.git("commit", "-qam", "incorrect clock", cwd=r)
        def hook(session):
            res = subprocess.run([sys.executable, str(CORE / "bin/brain"), "hook", "stop"], cwd=r,
                                 input=json.dumps({"cwd": str(r), "session_id": session}),
                                 capture_output=True, text=True, env=self.env)
            self.assertEqual(res.returncode, 0)
            return json.loads(res.stdout) if res.stdout.strip() else None
        first = hook("worker")
        self.assertIn("future Updated:", first["reason"])
        self.assertIn("if you are a worker, tell your coordinator", first["reason"])
        self.assertIsNone(hook("worker"))
        self.assertIsNotNone(hook("coordinator"))
        state = r / ".brain/hooks.json"
        reminders = json.loads(state.read_text())
        reminders["worker"] = "2020-01-01T00:00:00+00:00"
        state.write_text(json.dumps(reminders))
        self.assertIsNotNone(hook("worker"))


class JobLessonOutcomes(Scenario):
    def test_merge_requires_outcome_but_historical_rows_do_not(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(r, "new", "a", "--title", "A", "--home", str(home))
        led = r / "nodes/a/job-ledger.md"
        led.write_text("<!-- brain:job-ledger v1 -->\n\n| Job | Started | Ended | Hours | Outcome | Commits"
                       " | Reviews | Runtime | Cause |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
                       "| old | 2020-01-01T00:00Z | 2020-01-01T01:00Z | 1 | merged | 1 | 1 | codex |  |\n")
        self.assertNotIn("lesson outcome", self.b(r, "check"))
        self.b(r, "wt", "add", "a", "job")
        wt = self.tmp / "app-work/worktrees/job"
        self.git("commit", "--allow-empty", "-qm", "job", cwd=wt)
        self.git("merge", "-q", "job", cwd=home)
        self.b(r, "wt", "prune", "a")
        self.assertIn("merged job job has no lesson outcome", self.b(r, "check"))
        self.assertNotIn("merged job old has", self.b(r, "check"))
        for value in ("none", "none: ", "unknown"):
            self.b(r, "jobs", "a", "--branch", "job", "--lessons", value, ok=False)
        self.b(r, "jobs", "a", "--branch", "job", "--lessons", "none: mechanical change")
        self.assertNotIn("lesson outcome", self.b(r, "check"))
        self.assertIn("none: mechanical change", self.b(r, "jobs", "a", "--branch", "job"))
        self.assertNotIn("merged job old has", self.b(r, "check"))

    def test_learned_item_ids_and_switches_are_recorded_without_other_files(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(r, "new", "a", "--title", "A", "--home", str(home))
        self.b(r, "wt", "add", "a", "job|pipe")
        self.b(r, "item", "add", "method", "--from", "a", "--title", "Method", "--kind", "lesson",
               "--ref", "report@commit", "--body", "Keep a control.")
        st = r / "nodes/a/status.md"
        st.write_text(st.read_text() + "\nuncommitted coordinator work\n")
        self.b(r, "jobs", "a", "--branch", "job|pipe", "--lessons", "method",
               "--switch", "claudeAgent -> codex: usage limit")
        out = self.b(r, "jobs", "a", "--branch", "job|pipe")
        self.assertIn('"Lessons": "method"', out)
        self.assertIn("claudeAgent -> codex: usage limit", out)
        self.assertEqual(self.git("status", "--porcelain", "--", "nodes", cwd=r).strip(), "M nodes/a/status.md")
        self.b(r, "jobs", "--lessons", "method", ok=False)
        brief = self.b(r, "brief", "a", "--branch", "job|pipe")
        self.assertIn("must have a Lessons section", brief)
        self.assertIn("none: <reason>", brief)


class WorkerReadyPackets(Scenario):
    def job(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "app", {"calc.py": "value = 1\n",
                         "verify.py": "from calc import value\nassert value == 2\n"})
        self.b(r, "new", "a", "--title", "A", "--home", str(home))
        self.b(r, "set", "a", "verify=python3 -B verify.py")
        self.b(r, "wt", "add", "a", "job")
        wt = self.tmp / "app-work/worktrees/job"
        (wt / "calc.py").write_text("value = 2\n")
        self.git("commit", "-qam", "fix", cwd=wt)
        return r, home, wt

    def record(self, r, ok=True):
        return self.b(r, "ready", "a", "--branch", "job", "--record", "--reviews", "2", ok=ok)

    def packet(self):
        return next((self.tmp / "app-work/scratch/ready").glob("*.json"))

    def test_packet_records_executed_verify_and_checker_does_not_rerun(self):
        r, home, wt = self.job()
        self.assertIn("packet missing", self.b(r, "ready", "a", "--branch", "job", ok=False))
        before = self.git("rev-parse", "HEAD", cwd=r)
        self.assertEqual(self.record(r).strip(), "ready")
        p = json.loads(self.packet().read_text())
        self.assertEqual(p["head"], self.git("rev-parse", "job", cwd=home).strip())
        self.assertEqual(p["main"], self.git("rev-parse", "main", cwd=home).strip())
        self.assertEqual((p["verify_command"], p["verify_result"], p["review_rounds"]), ("python3 -B verify.py", 0, 2))
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=r), before)
        # A checker must not execute the command: make the executable unavailable in its PATH.
        original = self.env["PATH"]
        tools = self.tmp / "tools"
        tools.mkdir()
        (tools / "git").symlink_to(shutil.which("git"))
        self.env["PATH"] = str(tools)
        try:
            self.assertEqual(self.b(r, "ready", "a", "--branch", "job").strip(), "ready")
        finally:
            self.env["PATH"] = original
        state = json.loads(self.b(r, "ui", "--json"))
        node = next(n for n in state["nodes"] if n["id"] == "a")
        self.assertTrue(node["worktrees"][0]["ready"]["ready"])

    def test_main_move_and_head_change_require_worker_recheck(self):
        r, home, wt = self.job()
        self.record(r)
        (home / "other.txt").write_text("new main\n")
        self.git("add", "other.txt", cwd=home)
        self.git("commit", "-qm", "main moves", cwd=home)
        self.assertIn("main moved", self.b(r, "ready", "a", "--branch", "job", ok=False))
        self.assertIn("rebase on current main", self.record(r, ok=False))
        self.git("rebase", "main", cwd=wt)
        self.assertEqual(self.record(r).strip(), "ready")
        self.git("commit", "--allow-empty", "-qm", "amend job", cwd=wt)
        self.assertIn("branch head changed", self.b(r, "ready", "a", "--branch", "job", ok=False))

    def test_failed_verify_dirty_tree_missing_config_and_reviews(self):
        r, home, wt = self.job()
        (wt / "calc.py").write_text("value = 3\n")
        self.assertIn("dirty", self.record(r, ok=False))
        self.git("commit", "-qam", "wrong", cwd=wt)
        self.assertIn("verify missing, changed or failed", self.record(r, ok=False))
        self.assertNotEqual(json.loads(self.packet().read_text())["verify_result"], 0)
        self.b(r, "set", "a", "verify=")
        self.assertIn("no verify command", self.record(r, ok=False))
        self.b(r, "set", "a", "verify=true")
        self.assertIn("requires --reviews", self.b(r, "ready", "a", "--branch", "job", "--record", ok=False))
        self.assertIn("at least one", self.b(r, "ready", "a", "--branch", "job", "--record", "--reviews", "0", ok=False))

    def test_conflict_and_mutation_during_verify_cannot_be_ready(self):
        r, home, wt = self.job()
        (home / "calc.py").write_text("value = 9\n")
        self.git("commit", "-qam", "conflict", cwd=home)
        self.assertIn("merge-tree is not clean", self.record(r, ok=False))
        self.git("rebase", "--onto", "job", "main", "main", cwd=home)
        self.b(r, "set", "a", "verify=git commit --allow-empty -qm changed-during-verify")
        self.assertIn("branch head changed", self.record(r, ok=False))

    def test_malformed_or_incomplete_packets_fail_closed(self):
        r, home, wt = self.job()
        self.record(r)
        path = self.packet()
        original = json.loads(path.read_text())
        for data in ([1], {}, {**original, "review_rounds": 0}, {**original, "verify_result": "0"},
                     {**original, "checked_at": {}}, {**original, "node": "other"}):
            path.write_text(json.dumps(data))
            self.assertIn("not ready", self.b(r, "ready", "a", "--branch", "job", ok=False))
        path.write_text("{")
        self.assertIn("unreadable", self.b(r, "ready", "a", "--branch", "job", ok=False))


class ReadyFailureRegressions(Scenario):
    job = WorkerReadyPackets.job
    record = WorkerReadyPackets.record
    packet = WorkerReadyPackets.packet

    def merge_tree_exit(self, code):
        tools = self.tmp / "tools"
        tools.mkdir(exist_ok=True)
        script = tools / "git"
        script.write_text(f"#!{sys.executable}\nimport os, sys\nfrom pathlib import Path\n"
                          "if 'merge-tree' in sys.argv and '--write-tree' in sys.argv:\n"
                          f"    Path({str(self.tmp / 'merge-called')!r}).touch()\n"
                          f"    sys.exit({code})\n"
                          f"os.execv({shutil.which('git')!r}, ['git', *sys.argv[1:]])\n")
        script.chmod(0o755)
        self.env["PATH"] = str(tools) + os.pathsep + os.environ.get("PATH", "")

    def test_fast_forward_ready_does_not_require_modern_merge_tree(self):
        r, home, wt = self.job()
        self.merge_tree_exit(129)
        self.assertEqual(self.record(r).strip(), "ready")
        self.assertEqual(self.b(r, "ready", "a", "--branch", "job").strip(), "ready")
        self.assertFalse((self.tmp / "merge-called").exists())

    def test_ready_uses_local_main_without_updating_remote_refs(self):
        r, home, wt = self.job()
        local_main = self.git("rev-parse", "main", cwd=home).strip()
        self.git("checkout", "-qb", "remote-tip", cwd=home)
        self.git("commit", "--allow-empty", "-qm", "remote ahead", cwd=home)
        self.git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=home)
        remote_main = self.git("rev-parse", "HEAD", cwd=home).strip()
        self.git("checkout", "-q", "main", cwd=home)
        self.assertEqual(self.record(r).strip(), "ready")
        self.assertEqual(json.loads(self.packet().read_text())["main"], local_main)
        self.assertNotEqual(local_main, remote_main)
        self.assertEqual(self.git("rev-parse", "origin/main", cwd=home).strip(), remote_main)
        skill = (CORE / "skills/home-job/SKILL.md").read_text()
        self.assertIn("local `main` and never fetches", skill)
        self.assertIn("must fetch and update local `main` before `ready`", skill)

    def test_merge_tree_exit_codes_distinguish_conflict_old_git_and_error(self):
        r, home, wt = self.job()
        self.git("commit", "--allow-empty", "-qm", "main moves", cwd=home)
        for code, expected in ((129, "git is too old"), (128, "merge cleanliness is unknown"),
                               (1, "merge-tree is not clean")):
            self.merge_tree_exit(code)
            out = self.record(r, ok=False)
            self.assertIn("rebase on current main", out)
            self.assertIn(expected, out)
            if code != 1:
                self.assertNotIn("merge-tree is not clean", out)

    def test_failed_verify_prints_bounded_redacted_stdout_and_stderr(self):
        r, home, wt = self.job()
        (r / "nodes/a/never-name.txt").write_text("PrivateName\n")
        self.b(r, "set", "a", "verify=python3 -B fail.py")
        (wt / "fail.py").write_text("import sys\nprint('dropped from tail', flush=True)\n"
                                    "for i in range(20): print(f'context {i}', flush=True)\n"
                                    "print('PrivateName useful failure', file=sys.stderr)\nsys.exit(7)\n")
        self.git("add", "fail.py", cwd=wt)
        self.git("commit", "-qm", "failing verify", cwd=wt)
        out = self.record(r, ok=False)
        self.assertIn("verify failed (exit 7)", out)
        self.assertIn("context 19", out)
        self.assertIn("[…] useful failure", out)
        self.assertNotIn("PrivateName", out)
        self.assertNotIn("dropped from tail", out)
        self.assertNotIn("context 0\n", out)
        self.assertEqual(json.loads(self.packet().read_text())["verify_result"], 7)


class RuntimeFallbackResume(Scenario):
    def test_fallback_is_a_plain_machine_local_order(self):
        r = self.init("alice")
        head = self.git("rev-parse", "HEAD", cwd=r)
        self.b(r, "set", "root", "fallback=claudeAgent,codex")
        local = json.loads((r / ".brain/local.json").read_text())
        self.assertEqual(local["fallback"], ["claudeAgent", "codex"])
        self.assertNotIn("fallback:", (r / "node.md").read_text())
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=r), head)
        self.assertIn('["claudeAgent", "codex"]', self.b(r, "machine"))
        self.assertEqual(json.loads(self.b(r, "ui", "--json"))["brain"]["fallback"], local["fallback"])
        self.b(r, "set", "root", "fallback=codex,codex", ok=False)
        self.b(r, "set", "root", "fallback=codex,,claudeAgent", ok=False)
        self.b(r, "new", "a", "--title", "A")
        self.b(r, "set", "a", "fallback=codex", ok=False)
        local["fallback"] = {"first": "codex"}
        (r / ".brain/local.json").write_text(json.dumps(local))
        self.assertIn("fallback must be an ordered list", self.b(r, "check", ok=False))
        self.b(r, "set", "root", "fallback=")
        self.assertNotIn("fallback must", self.b(r, "check"))

    def test_partial_branch_brief_carries_resume_and_ledger_state(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(r, "new", "a", "--title", "A", "--home", str(home))
        self.b(r, "wt", "add", "a", "job")
        self.assertNotIn("## Resume existing work", self.b(r, "brief", "a", "--branch", "job"))
        wt = self.tmp / "app-work/worktrees/job"
        self.git("commit", "--allow-empty", "-qm", "partial", cwd=wt)
        sha = self.git("rev-parse", "HEAD", cwd=wt).strip()
        self.b(r, "jobs", "a", "--branch", "job", "--switch", "claudeAgent -> codex: usage limit")
        brief = self.b(r, "brief", "a", "--branch", "job", "--runtime", "codex", "--task", "Finish it")
        self.assertIn("Continue from these commits", brief)
        self.assertIn(sha, brief)
        self.assertIn("do not reset, recreate the branch", brief)
        self.assertIn("scratch/ready/", brief)
        self.assertIn("Job ledger state:", brief)
        self.assertIn("claudeAgent -> codex: usage limit", brief)
        self.assertIn("runtime: codex", brief)
        self.assertIn("Finish it", brief)


class ResumeBriefRegressions(Scenario):
    def job(self, base):
        r = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.git("checkout", "-qb", "feature", cwd=home)
        self.git("commit", "--allow-empty", "-qm", "inherited work", cwd=home)
        if base == "origin/main":
            self.git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=home)
            self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=home)
            self.git("checkout", "-q", "main", cwd=home)
        self.b(r, "new", "a", "--title", "A", "--home", str(home))
        self.b(r, "wt", "add", "a", "job")
        return r, self.tmp / "app-work/worktrees/job"

    def test_fresh_remote_base_does_not_resume_inherited_commits(self):
        r, wt = self.job("origin/main")
        self.assertNotIn("## Resume existing work", self.b(r, "brief", "a", "--branch", "job"))
        self.git("commit", "--allow-empty", "-qm", "own work", cwd=wt)
        self.assertIn("1 own commit(s)", self.b(r, "brief", "a", "--branch", "job"))

    def test_fresh_feature_head_base_does_not_resume_inherited_commits(self):
        r, wt = self.job("HEAD")
        self.assertNotIn("## Resume existing work", self.b(r, "brief", "a", "--branch", "job"))
        self.git("commit", "--allow-empty", "-qm", "own work", cwd=wt)
        self.assertIn("1 own commit(s)", self.b(r, "brief", "a", "--branch", "job"))

    def test_uncommitted_work_resumes_before_first_commit(self):
        r, wt = self.job("HEAD")
        (wt / "partial.txt").write_text("unfinished work\n")
        brief = self.b(r, "brief", "a", "--branch", "job")
        self.assertIn("## Resume existing work", brief)
        self.assertIn("0 own commit(s) and uncommitted changes", brief)
        self.b(r, "jobs", "a", "--branch", "job", "--switch", "claude -> codex: usage limit")
        self.assertIn("claude -> codex: usage limit", self.b(r, "brief", "a", "--branch", "job"))

    def test_switch_or_review_alone_resumes_before_first_commit(self):
        r, wt = self.job("HEAD")
        self.b(r, "review", "a", "--branch", "job", "--built-by", "claude")
        self.assertIn("## Resume existing work", self.b(r, "brief", "a", "--branch", "job"))
        self.b(r, "wt", "add", "a", "switched")
        self.b(r, "jobs", "a", "--branch", "switched", "--switch", "claude -> codex: usage limit")
        brief = self.b(r, "brief", "a", "--branch", "switched")
        self.assertIn("## Resume existing work", brief)
        self.assertIn("0 own commit(s)", brief)


class Measures(Scenario):
    def test_jobs_are_measured_from_start_to_end(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, "wt", "add", "app", "job-a")
        self.b(r, "wt", "add", "app", "job-b")
        jobs = (r / "nodes" / "app" / "job-ledger.md").read_text()
        self.assertIn("| job-a |", jobs)
        self.assertEqual(self.git("status", "--porcelain", "--", "nodes/app/job-ledger.md", cwd=r).strip(), "")  # committed
        self.b(r, "review", "app", "--branch", "job-a", "--built-by", "claude")
        self.b(r, "review", "app", "--branch", "job-a", "--built-by", "claude")
        wa = self.tmp / "code" / "app-work" / "worktrees" / "job-a"
        (wa / "a.txt").write_text("a\n")
        self.git("add", "a.txt", cwd=wa); self.git("commit", "-qm", "A", cwd=wa)
        self.git("merge", "-q", "job-a", cwd=home)
        self.assertIn("removed: job-a", self.b(r, "wt", "prune", "app"))
        self.b(r, "wt", "rm", "app", "job-b", "--cause", "superseded by job-a")
        rows = {l.split("|")[1].strip(): [c.strip() for c in l.split("|")[1:-1]]
                for l in (r / "nodes" / "app" / "job-ledger.md").read_text().splitlines() if l.startswith("| job-")}
        self.assertEqual(rows["job-a"][4], "merged")
        self.assertEqual(rows["job-a"][5], "1")                 # commits
        self.assertEqual(rows["job-a"][6], "2")                 # review rounds
        self.assertEqual(rows["job-b"][4], "abandoned")
        self.assertEqual(rows["job-b"][8], "superseded by job-a")
        out = self.b(r, "jobs", "app")
        self.assertIn("2 finished (1 merged, 1 abandoned)", out)
        self.assertIn("2 review round(s) per merged job", out)
        self.assertIn("cause superseded by job-a", out)
        self.b(r, "wt", "add", "app", "job-c")
        self.assertIn("open  job-c", self.b(r, "jobs", "app"))
        self.assertIn("✓", self.b(r, "check"))
        self.b(r, "wt", "add", "app", "job|pipe")                  # git allows it; the row must still be found
        self.b(r, "review", "app", "--branch", "job|pipe", "--built-by", "claude")
        self.b(r, "wt", "rm", "app", "job|pipe")
        self.assertIn("job|pipe", (r / "nodes" / "app" / "job-ledger.md").read_text().replace("\\|", "|"))
        self.assertNotIn("open  job|pipe", self.b(r, "jobs", "app"))

    def test_a_file_brain_did_not_write_is_never_overwritten(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        mine = r / "nodes" / "app" / "job-ledger.md"
        mine.write_text("# My own notes\n\nAcceptance: the export must round-trip.\n")
        out = self.b(r, "wt", "add", "app", "job-a")
        self.assertIn("isn't a job ledger written by brain", out)
        self.assertEqual(mine.read_text(), "# My own notes\n\nAcceptance: the export must round-trip.\n")

    def test_medians_and_daylight_saving(self):
        from importlib.machinery import SourceFileLoader
        import datetime as dt
        from zoneinfo import ZoneInfo
        m = SourceFileLoader("brain_mod", str(CORE / "bin" / "brain")).load_module()
        ny = ZoneInfo("America/New_York")
        a, b = dt.datetime(2026, 3, 8, 1, 30, tzinfo=ny), dt.datetime(2026, 3, 8, 3, 30, tzinfo=ny)
        self.assertEqual(m.elapsed(a, b), 3600)                    # clocks jump forward: one real hour
        r = self.init("alice")
        self.b(r, "new", "app", "--title", "App")
        led = r / "nodes" / "app" / "job-ledger.md"
        led.write_text("# Jobs: app\n\n<!-- brain:job-ledger v1 -->\n\n| Job | Started | Ended | Hours | Outcome | Commits"
                       " | Reviews | Runtime | Cause |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
                       "| a | 2026-01-01T00:00+00:00 | 2026-01-01T01:00+00:00 | 1.0 | merged | 1 | 1 | claude |  |\n"
                       "| b | 2026-01-01T00:00+00:00 | 2026-01-01T09:00+00:00 | 9.0 | merged | 1 | 3 | claude |  |\n")
        self.assertIn("median 5.0 h and 2 review round(s)", self.b(r, "jobs", "app"))

    def test_jobs_survive_commands_running_at_once(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, "wt", "add", "app", "job-a")
        st = r / "nodes" / "app" / "status.md"
        st.write_text(st.read_text() + "\n- the coordinator's edit, not committed yet\n")
        ps = [subprocess.Popen([sys.executable, str(r / "core" / "bin" / "brain"), "review", "app", "--branch", "job-a",
                                "--built-by", "claude"], cwd=r, env=self.env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True) for _ in range(16)]
        errs = [p.communicate()[1] for p in ps]
        self.assertEqual([p.returncode for p in ps], [0] * 16, errs)
        row = next(l for l in (r / "nodes" / "app" / "job-ledger.md").read_text().splitlines() if l.startswith("| job-a"))
        self.assertEqual(row.split("|")[7].strip(), "16")         # no round lost
        self.assertEqual(self.git("status", "--porcelain", "--", "nodes", cwd=r).strip(), "M nodes/app/status.md")

    def test_stale_compares_moments_not_digits(self):
        r = self.init("alice")
        self.b(r, "new", "app", "--title", "App")
        inst = r / "instance.md"
        inst.write_text(inst.read_text().replace("timezone: UTC", "timezone: Asia/Beirut"))
        st = r / "nodes" / "app" / "status.md"
        lines = st.read_text().splitlines()
        lines[0] = "Updated: 2026-10-19 06:31 by app coordinator (claude)"   # Beirut, UTC+3: 03:31Z
        st.write_text("\n".join(lines) + "\n")
        self.assertIn("current", self.b(r, "stale", "app", "2026-10-19T03:20:00Z"))     # looks later, isn't
        out = self.b(r, "stale", "app", "2026-10-19T05:00:00.000Z")
        self.assertIn("behind by 1h 29m", out)
        self.assertIn("node-sync", out)
        self.assertIn("current", self.b(r, "stale", "app", "2026-10-19T06:33:00+03:00"))  # within the grace
        self.b(r, "stale", "app", "2026-10-19 05:00", ok=False)                          # no zone: refused

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
        self.assertIn({"node": "app", "kind": "blocked", "text": "The API key"}, port["attention"])
        self.assertGreaterEqual(sum(port["activity"]["counts"].get("app", [])), 1)   # "app: …" commits count for app
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            free = sk.getsockname()[1]
        p = subprocess.Popen([sys.executable, str(r / "core" / "bin" / "brain"), "ui", "--port", str(free),
                              "--every", "0.5"], cwd=r, env=self.env, stdout=subprocess.PIPE, text=True)
        try:
            line = p.stdout.readline()
            full = line.split(": ", 1)[1].split()[0]
            self.assertTrue(full.startswith("http://127.0.0.1:"))
            url, key = full.split("?k=")
            self.assertIn(b"<title>", urllib.request.urlopen(url, timeout=5).read())
            with self.assertRaises(urllib.error.HTTPError) as e:   # another account on this machine has no key
                urllib.request.urlopen(url + "api/state", timeout=5)
            self.assertEqual(e.exception.code, 403)
            state = json.loads(urllib.request.urlopen(url + "api/state?k=" + key, timeout=5).read())
            self.assertEqual(state["brain"]["name"], "alice")
            req = urllib.request.Request(url + "api/state?k=" + key, headers={"Host": "evil.example"})
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(e.exception.code, 403)              # another site can't read it through a browser
            ev = urllib.request.urlopen(url + "api/events?k=" + key, timeout=10)
            first = ev.readline()
            self.assertTrue(first.startswith(b"data: {"))
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

class Review(Scenario):
    def test_review_brief_goes_to_the_other_runtime(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        home = self.repo(self.tmp / "app")
        self.b(r, "new", "app", "--title", "App", "--owners", "team", "--home", str(home))
        self.b(r, "wt", "add", "app", "fix-login", "--base", "main")
        wt = self.tmp / "app-work" / "worktrees" / "fix-login"
        (wt / "login.py").write_text("def login(u, p):\n    return True\n")
        self.git("add", "-A", cwd=wt); self.git("commit", "-qm", "Fix login", cwd=wt)
        out = self.b(r, "review", "app", "--branch", "fix-login", "--built-by", "claude", "--focus", "auth")
        self.assertIn("Reviewer runtime: codex", out)
        self.assertIn(f"Run in: {wt}", out)
        self.assertIn("login.py", out)
        self.assertIn("Don't modify any file", out)
        self.assertIn("has its own owners", out)
        self.assertNotIn(str(r), out)                        # nothing from above the boundary
        self.assertIn("Reviewer runtime: claude",
                      self.b(r, "review", "app", "--branch", "fix-login", "--built-by", "codex"))
        self.b(r, "review", "app", "--branch", "fix-login", "--built-by", "codex", "--by", "codex", ok=False)


class Schedules(Scenario):
    def test_schedules_are_shared_and_bound_per_machine(self):
        r = self.init("alice")
        self.b(r, "schedule", "add", "daily-brief", "--when", "daily 09:00", "--brief", "Run the node-brief skill.")
        self.assertIn("schedule: daily-brief", self.git("log", "-1", "--format=%s", cwd=r))
        self.assertIn("not on this machine", self.b(r, "schedule"))
        self.b(r, "schedule", "bind", "daily-brief", "host-42")
        self.assertIn("on this machine: host-42", self.b(r, "schedule", "list"))
        self.assertNotIn("host-42", self.git("show", "HEAD", cwd=r))       # the binding isn't shared
        self.assertIn("daily-brief                  on this machine", self.b(r, "machine"))
        self.assertIn("✓", self.b(r, "check"))
        (r / "schedules.md").write_text((r / "schedules.md").read_text().replace("node: root", "node: ghost"))
        self.assertIn("unknown node 'ghost'", self.b(r, "check", ok=False))

    def test_sync_all_covers_every_external_home(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        for nid in ("one", "two"):
            home = self.repo(self.tmp / nid, {"README.md": f"{nid}\n"})
            self.b(r, "new", nid, "--title", nid, "--owners", "team", "--home", str(home))
        self.b(r, "new", "local", "--title", "Local")
        out = self.b(r, "sync", "--all", "--offline")
        self.assertIn("# one:", out)
        self.assertIn("# two:", out)
        self.assertNotIn("# local:", out)


class StopHook(Scenario):
    def hook(self, r, cwd, session="s1", active=False):
        """Run the exact command the runtimes run, as they do: hook JSON on stdin, in the session's folder."""
        cmd = json.loads((CORE / "adapters" / "runtimes" / "claude-hooks.json").read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
        env = {k: v for k, v in self.env.items() if k != "BRAIN_HOOK_REMIND_MINUTES"}
        res = subprocess.run(["sh", "-c", cmd], cwd=cwd, env=env, capture_output=True, text=True,
                             input=json.dumps({"cwd": str(cwd), "session_id": session, "stop_hook_active": active,
                                               "hook_event_name": "Stop"}))
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout) if res.stdout.strip() else None

    def test_links_for_both_runtimes(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        for f in (r / ".claude" / "settings.json", r / "nodes" / "a" / ".claude" / "settings.json",
                  r / ".codex" / "hooks.json"):
            self.assertTrue(f.is_symlink() and f.exists(), f)
        self.assertFalse((r / "nodes" / "a" / ".codex").exists(), "codex reads hooks from the root only")
        (r / "nodes" / "a" / ".claude" / "settings.json").unlink()
        self.assertIn("stop hook) is missing", self.b(r, "check", ok=False))
        self.b(r, "check", "--fix")
        self.assertIn("✓", self.b(r, "check"))

    def test_stop_hook_holds_a_turn_until_the_notes_are_current(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        a = r / "nodes" / "a"
        self.assertIsNone(self.hook(r, a), "fresh, committed notes: let it stop")
        time.sleep(1.1)
        (a / "brief.md").write_text((a / "brief.md").read_text() + "\nAudience: teenagers\n")
        out = self.hook(r, a)
        self.assertEqual(out["decision"], "block")
        self.assertIn("changed after status.md: brief.md", out["reason"])
        self.assertIn("not committed", out["reason"])
        self.assertIsNone(self.hook(r, a, active=True), "never twice in a row")
        st = a / "status.md"
        st.write_text(st.read_text().replace(st.read_text().splitlines()[0],
                                             "Updated: " + self.b(r, "now").strip() + " by a coordinator (claude)"))
        self.git("add", "-A", cwd=r); self.git("commit", "-qm", "a: brief", cwd=r)
        self.assertIsNone(self.hook(r, a))
        self.assertIsNone(self.hook(r, self.tmp), "outside an instance: nothing")
        self.assertIn("stop a s1", (r / ".brain" / "hooks.log").read_text())
        self.assertNotIn("hooks", self.git("ls-files", ".brain", cwd=r))

    def test_old_notes_get_one_reminder_per_session(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        a, st = r / "nodes" / "a", r / "nodes" / "a" / "status.md"
        lines = st.read_text().splitlines()
        lines[0] = "Updated: 2020-01-01 00:00 by a coordinator (claude)"
        st.write_text("\n".join(lines) + "\n")
        self.git("commit", "-qam", "a: old", cwd=r)
        self.assertIn("last updated", self.hook(r, a, "s1")["reason"])
        self.assertIsNone(self.hook(r, a, "s1"), "reminded once this hour")
        self.assertIsNotNone(self.hook(r, a, "s2"), "another session gets its own reminder")


class InstallAndUpgrade(Scenario):
    def core_repo(self):
        src = self.tmp / "core-src"
        shutil.copytree(CORE, src, ignore=shutil.ignore_patterns(".git", "__pycache__", ".DS_Store"))
        self.git("init", "-q", "-b", "main", cwd=src)
        self.git("add", "-A", cwd=src)
        self.git("commit", "-qm", "core", cwd=src)
        return src

    def install(self, *args):
        r = subprocess.run(["sh", str(CORE / "install.sh"), "--yes", *args], cwd=self.tmp,
                           env={**self.env, "SHELL": "/bin/zsh"},
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if r.returncode:
            self.fail(f"install.sh failed:\n{r.stdout}\n{r.stderr}")
        return r.stdout + r.stderr

    def test_install_upgrade_and_second_machine(self):
        src = self.core_repo()
        bob = self.tmp / "Brain"
        out = self.install("--operator", "bob", "--title", "Bob", "--runtimes", "claude,codex", "--tz", "UTC",
                           "--core-url", str(src), "--bin-dir", str(self.tmp / "bin"))
        self.assertIn("✓ checks pass", out)
        self.assertIn("path = core", (bob / ".gitmodules").read_text())
        cmd = self.tmp / "bin" / "brain"
        self.assertTrue(cmd.is_symlink())
        env = {**self.env}
        env.pop("BRAIN_NO_REEXEC")
        t = subprocess.run([str(cmd), "tree"], cwd=self.tmp, env=env, capture_output=True, text=True)
        self.assertIn("root: Bob", t.stdout, t.stderr)
        rc = self.tmp / ".zshrc"
        self.assertIn('export PATH="$HOME/bin:$PATH"', rc.read_text())
        self.assertIn("Brain is at `~/Brain`", (self.tmp / ".claude" / "CLAUDE.md").read_text())
        self.assertNotIn("to PATH)", out)
        self.assertIn("Open a new terminal", out)

        # a second brain on this machine: its own folder and config folder, the same command
        self.install("--dir", "~/Work", "--name", "work", "--operator", "bob", "--title", "Work", "--runtimes",
                     "claude", "--tz", "UTC", "--core-url", str(src), "--bin-dir", str(self.tmp / "bin"),
                     "--global", "claude=~/.claude-work")
        self.assertEqual(os.path.realpath(cmd), str((bob / "core" / "bin" / "brain").resolve()))
        self.assertNotIn("Work", (self.tmp / ".claude" / "CLAUDE.md").read_text())
        self.assertIn("`brain -b work`", (self.tmp / ".claude-work" / "CLAUDE.md").read_text())
        t = subprocess.run([str(cmd), "-b", "work", "tree"], cwd=self.tmp, env=env, capture_output=True, text=True)
        self.assertIn("root: Work", t.stdout, t.stderr)
        t = subprocess.run([str(cmd), "tree"], cwd=self.tmp, env=env, capture_output=True, text=True)
        self.assertIn("root: Bob", t.stdout, t.stderr)
        self.assertIn("another Brain: --dir",
                      subprocess.run(["sh", str(CORE / "install.sh"), "--yes", "--title", "X", "--core-url", str(src)],
                                     cwd=self.tmp, env=self.env, capture_output=True, text=True).stderr)

        # a sealed node, so the second machine has a home to bind
        self.b(bob, "principal", "add", "team")
        code = self.repo(self.tmp / "code")
        self.b(bob, "new", "code", "--title", "Code", "--owners", "team", "--home", str(code))

        # upgrade: a new core commit reaches the instance
        (src / "README.md").write_text((src / "README.md").read_text() + "\nUPGRADED\n")
        self.git("commit", "-qam", "Improve the README", cwd=src)
        out = self.b(bob, "upgrade")
        self.assertIn("Improve the README", out)
        self.assertIn("UPGRADED", (bob / "core" / "README.md").read_text())
        self.assertIn("Upgrade core", self.git("log", "-1", "--format=%s", cwd=bob))
        self.assertIn("up to date", self.b(bob, "upgrade"))

        # second machine: same Brain, its own bindings
        bob2 = self.tmp / "Brain2"
        out = self.install("--from", str(bob), "--dir", str(bob2), "--home", f"code={code}",
                           "--bin-dir", str(self.tmp / "bin2"), "--no-modify-path")
        self.assertIn("✓ checks pass", out)
        self.assertIn("UPGRADED", (bob2 / "core" / "README.md").read_text())
        self.assertEqual(json.loads((bob2 / ".brain" / "local.json").read_text())["nodes"]["code"]["home"], str(code))
        self.assertIn("coordinator MISSING", self.b(bob2, "machine"))

        bob3 = self.tmp / "Brain3"
        out = self.install("--from", str(bob), "--dir", str(bob3), "--bin-dir", str(self.tmp / "bin"))
        self.assertIn("code: home not set", out)
        self.assertNotIn("bin2", rc.read_text())
        self.assertEqual(rc.read_text().count("# brain"), 1)
        self.assertIn("home not set on this machine", self.b(bob3, "check", ok=False))


if __name__ == "__main__":
    unittest.main()
