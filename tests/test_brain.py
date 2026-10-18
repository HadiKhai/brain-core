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
        self.env.pop("BRAIN_ROOT", None)

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
        self.assertIn(f'export PATH="{self.tmp / "bin"}:$PATH"', rc.read_text())
        self.assertIn("Open a new terminal", out)

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
