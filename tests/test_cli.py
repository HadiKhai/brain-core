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


class SettlementDocsHelp(unittest.TestCase):
    def test_settlement_guides_require_export_for_each_recording_command(self):
        for name in ('roles/coordinator.md', 'skills/home-job/SKILL.md',
                     'adapters/hosts/t3.md', 'README.md', 'CHANGES.md'):
            text = (CORE / name).read_text()
            commands = re.findall(r'`(brain jobs [^`]*--settled[^`]*)`', text)
            self.assertTrue(commands, name)
            for command in commands:
                with self.subTest(document=name, command=command):
                    self.assertIn('--attestation-file', command)
        for name in ('roles/coordinator.md', 'skills/home-job/SKILL.md'):
            text = (CORE / name).read_text()
            self.assertIn('version 1', text)
            self.assertIn('five minutes', ' '.join(text.split()))
            self.assertIn('--job-key <key>', text)

    def test_jobs_help_describes_settlement_export_and_selection(self):
        result = subprocess.run([sys.executable, str(CORE / 'bin/brain'), 'jobs', '--help'],
                                cwd=CORE, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        help_text = ' '.join(result.stdout.split())
        for clause in ('--settled', '--all-closed', '--attestation-file',
                       'fresh version 1 coordinator export',
                       'exhaustive per-job/thread/project coverage', '--job-key'):
            self.assertIn(clause, help_text)


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


    def test_launcher_leaves_core_clean_without_bytecode_environment(self):
        core = self.tmp / "core-copy"
        # Model an installed core: UI development dependencies and build caches do not ship.
        shutil.copytree(CORE, core, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".DS_Store", "node_modules", "*.tsbuildinfo"))
        self.git("init", "-q", "-b", "main", cwd=core)
        self.git("add", "-A", cwd=core)
        self.git("commit", "-qm", "fixture core", cwd=core)
        env = dict(self.env)
        env.pop("PYTHONPYCACHEPREFIX", None)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        # Also force beside-code caches: some system Pythons redirect them even
        # without either environment variable, hiding writes in an installed core.
        beside_code = ("import runpy, sys; sys.pycache_prefix = None; "
                       "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')")
        for prefix in ([], ["-c", beside_code]):
            with self.subTest(prefix=prefix):
                result = subprocess.run([sys.executable, *prefix, str(core / "bin/brain"), "--help"],
                                        cwd=core, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all", "--ignored", cwd=core), "")
                self.assertEqual(list((core / "lib").rglob("__pycache__")), [])



    def test_policy_and_complete_rule_snapshot_cli_boundaries(self):
        r = self.init('alice')
        self.b(r, 'new', 'demo', '--title', 'Demo')
        node = r / 'nodes/demo'
        (node / 'brief.md').write_text('## Limits\n- Never open a pull request.\n')
        policy = json.loads(self.b(r, 'policy', 'demo', '--json'))
        reviewed_at = '2026-01-01 00:00 +00:00'  # fixed synthetic review evidence
        policy.update(reviewer='alice', reviewed_at=reviewed_at)
        for rule in policy['rules']:
            rule['actions'] = {'push': 'deny', 'pr': 'deny'}
        (node / 'publication-policy.json').write_text(json.dumps(policy))
        paths = [node / 'node.md', r / 'registry.md', r / '.brain/local.json']
        before = [p.read_bytes() for p in paths]
        refused = self.b(r, 'set', 'demo', 'coordinator=demo-thread', 'merge=pr', ok=False)
        self.assertIn('Never open a pull request.', refused)
        self.assertIn('brief.md', refused)
        self.assertEqual(before, [p.read_bytes() for p in paths])
        self.b(r, 'set', 'demo', 'merge=local')
        (node / 'brief.md').write_text('## Limits\n- Ask about each outward action.\n')
        unknown_before = [p.read_bytes() for p in paths]
        self.assertIn('coordinator', self.b(r, 'set', 'demo', 'merge=pr', ok=False))
        self.assertEqual(unknown_before, [p.read_bytes() for p in paths])
        snapshot = {'version': 1, 'node': 'demo', 'scope': ['alice'], 'importer': 'worker-1',
                    'importer_runtime': 'codex', 'provenance': 'example@abc',
                    'rules': [{'id': 'examples', 'text': 'Use fabricated examples.', 'source': 'example@abc:1'}]}
        source = node / 'sources/import-proposals.md'
        source.write_text(json.dumps(snapshot))
        sheet = json.loads(self.b(r, 'proposals', 'demo', '--json'))
        digest = sheet[0]['rules_snapshot']['digest']
        due = json.loads(self.b(r, 'due', 'demo', '--json'))
        activation = [i for i in due if i['kind'] == 'rule-activation-review']
        self.assertEqual(len(activation), 1)
        manifest = {**snapshot, 'snapshot_hash': digest, 'reviewer': 'alice',
                    'reviewer_runtime': 'claude', 'reviewed_at': reviewed_at}
        review = r / 'review.json'
        review.write_text(json.dumps(manifest))
        original = source.read_bytes()
        out = self.b(r, 'proposals', 'demo', '--approve-all', '--review-manifest', str(review))
        self.assertIn('supplied evidence', out)
        self.assertIn('Use fabricated examples.', (node / 'AGENTS.md').read_text())
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(json.loads(self.b(r, 'proposals', 'demo', '--json')), [])
        self.assertNotIn('rule-activation-review', [i['kind'] for i in json.loads(self.b(r, 'due', 'demo', '--json'))])
        self.assertIn('recorded open job', self.b(r, 'land-check', 'demo', '--branch', 'absent', '--run', ok=False))


if __name__ == "__main__":
    unittest.main()
