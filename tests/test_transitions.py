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


class Sealing(Scenario):
    def test_seal_moves_shared_material_out_and_leaves_no_trace(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "bob")
        self.b(r, "new", "venture", "--title", "Venture")
        (r / "nodes" / "venture" / "sources" / "plan.md").write_text("the shared plan\n")
        (r / "nodes" / "venture" / "status.md").write_text("Updated: x\nPRIVATE-ALICE thoughts\n")
        self.b(r, "status", "venture", "--refresh")
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
            self.assertNotIn(str(shared), (r / "nodes" / "proj" / "node.md").read_text())
            st = r / "nodes" / "proj" / "status.md"
            st.write_text(f"Updated: 2026-01-01 09:00 by proj coordinator (claude)\nPRIVATE-{me.upper()} opinion\n")
            self.b(r, "status", "proj", "--refresh")
            self.git("commit", "-qam", "proj: status", cwd=r)
            brains[me] = (r, clone)

        # ana's worker does a job in the shared repo, on a branch
        hr, hclone = brains["ana"]
        self.git("checkout", "-q", "-b", "auth-tests", cwd=hclone)
        (hclone / "test_auth.py").write_text("def test_guard():\n    assert True\n")
        self.git("add", "-A", cwd=hclone)
        # Git dates have second resolution. Put this fixture commit strictly
        # between the initial status boundary and the later sync mark.
        with mock.patch.dict(self.env, {'GIT_AUTHOR_DATE': '2026-01-02T09:00:00+00:00',
                                       'GIT_COMMITTER_DATE': '2026-01-02T09:00:00+00:00'}):
            self.git("commit", "-qm", "Add auth guard tests", cwd=hclone)
        self.git("push", "-q", "origin", "auth-tests", cwd=hclone)

        # ben's Brain learns about it only through the repo
        rr, rclone = brains["ben"]
        out = self.b(rr, "sync", "proj")
        self.assertIn("Add auth guard tests", out)
        self.assertIn("auth-tests", out)
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



if __name__ == "__main__":
    unittest.main()
