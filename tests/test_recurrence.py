"""Recurrence uses fabricated lessons and never edits approved direction."""
import argparse
import importlib.util
import json
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class LessonRecurrence(Scenario):
    def fixture(self):
        r = self.init("alice")
        self.b(r, "new", "app", "--title", "App")
        self.b(r, "specialist", "new", "editor", "--title", "Editor")
        folder = r / "specialists/editor"
        (folder / "direction.md").write_text(
            "# Editor\n## Principles\n### one-place: One thing, one place\nenforced_by: wish\n"
            "### bounded: Bound output\nenforced_by: checks/bounds.py\n")
        return r, folder

    def record(self, r, text, principle="one-place", target="editor", node=False):
        self.b(r, "lesson", target, *(["--node"] if node else []), "--from", "app",
               "--principle", principle, text)

    def notices(self, r, node="root"):
        return [i for i in json.loads(self.b(r, "due", node, "--json")) if i["kind"] == "synthesis"]

    def load_core(self):
        loader = core_loader.SourceFileLoader("recurrence_core", str(CORE / "bin/brain"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod

    def test_three_same_principle_and_new_sets_leave_originals_untouched(self):
        r, folder = self.fixture()
        for text in ("the title is repeated", "the subtitle restates the header"):
            self.record(r, text)
        self.assertEqual(self.notices(r), [])
        self.record(r, "the breadcrumb duplicates the navbar")
        originals = {f: (folder / f).read_bytes() for f in ("lessons.md", "role.md", "direction.md")}
        notices = self.notices(r)
        self.assertEqual(len(notices), 1)
        notice = notices[0]
        self.assertIn("synthesis due for specialist editor", notice["text"])
        self.assertEqual(notice["count"], 3)
        self.assertEqual(self.notices(r), [notice])
        self.b(r, "due", "root", "--synthesis-seen", notice["set"])
        self.assertEqual(self.notices(r), [])
        for f, content in originals.items():
            self.assertEqual((folder / f).read_bytes(), content)
        receipt = (folder / "synthesis.json").read_text()
        self.assertNotIn("title", receipt)
        self.assertNotIn("app", receipt)
        self.assertEqual(len(json.loads(receipt)["seen"]), 3)
        self.assertIsNotNone(self.load_core().Instance(r).parse_time(json.loads(receipt)["at"]))
        for i in range(2):
            self.record(r, "new correction " + str(i))
        self.assertEqual(self.notices(r), [])
        self.record(r, "third fresh correction")
        new, = self.notices(r)
        self.assertEqual(new["count"], 3)
        self.assertNotEqual(new["set"], notice["set"])

    def test_five_legacy_unclassified_without_direction(self):
        r, folder = self.fixture()
        (folder / "direction.md").unlink()
        legacy = "".join(f"- 2026-01-01 12:00 UTC · from app · scope alice: old {i}\n" for i in range(4))
        path = folder / "lessons.md"
        path.write_text("# Lessons\n" + legacy)
        self.assertEqual(self.notices(r), [])
        self.record(r, "new unclassified", "unclassified")
        before = path.read_bytes()
        notice, = self.notices(r)
        self.assertIn("5 lessons for unclassified", notice["text"])
        self.b(r, "due", "root", "--synthesis-seen", notice["set"])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((folder / "direction.md").exists())
        self.assertEqual(self.notices(r), [])

    def test_different_principles_and_targets_never_pool(self):
        r, folder = self.fixture()
        node = r / "nodes/app"
        (node / "direction.md").write_bytes((folder / "direction.md").read_bytes())
        for i in range(2):
            self.record(r, "same " + str(i))
            self.record(r, "other " + str(i), "bounded")
            self.record(r, "node " + str(i), target="app", node=True)
        self.assertEqual(self.notices(r), [])
        self.assertEqual(self.notices(r, "app"), [])
        self.record(r, "third other", "bounded")
        notice, = self.notices(r)
        self.assertIn("bounded", notice["text"])
        self.record(r, "third node", target="app", node=True)
        node_notice, = self.notices(r, "app")
        self.assertIn("synthesis due for node app", node_notice["text"])
        self.b(r, "due", "app", "--synthesis-seen", node_notice["set"])
        self.assertEqual(self.notices(r, "app"), [])
        self.assertEqual(self.notices(r), [notice])

    def test_stale_foreign_and_invalid_receipts_do_not_consume(self):
        r, folder = self.fixture()
        for i in range(3):
            self.record(r, "correction " + str(i))
        notice, = self.notices(r)
        self.b(r, "due", "app", "--synthesis-seen", notice["set"], ok=False)
        self.b(r, "due", "--synthesis-seen", notice["set"], ok=False)
        self.b(r, "due", "root", "--json", "--synthesis-seen", notice["set"], ok=False)
        self.record(r, "arrived during review")
        self.b(r, "due", "root", "--synthesis-seen", notice["set"], ok=False)
        self.assertFalse((folder / "synthesis.json").exists())
        fresh, = self.notices(r)
        self.assertEqual(fresh["count"], 4)
        self.b(r, "due", "root", "--synthesis-seen", fresh["set"])
        self.b(r, "due", "root", "--synthesis-seen", fresh["set"], ok=False)

    def test_reordering_and_identical_occurrences_have_stable_identity(self):
        r, folder = self.fixture()
        # Identical lines are possible: lesson capture timestamps have minute precision.
        for _ in range(3):
            self.record(r, "same correction")
        notice, = self.notices(r)
        self.assertEqual(notice["count"], 3)
        path = folder / "lessons.md"
        lines = path.read_text().splitlines()
        path.write_text("\n".join(reversed(lines)) + "\n")
        self.assertEqual(self.notices(r), [notice])
        self.b(r, "due", "root", "--synthesis-seen", notice["set"])
        for _ in range(2):
            self.record(r, "same correction")
        self.assertEqual(self.notices(r), [])
        self.record(r, "same correction")
        again, = self.notices(r)
        self.assertEqual(again["count"], 3)
        self.assertNotEqual(again["set"], notice["set"])

    def test_source_policy_scope_and_damaged_records_are_not_synthesized(self):
        r, folder = self.fixture()
        self.b(r, "principal", "add", "team")
        self.b(r, "new", "team", "--title", "Team", "--owners", "team")
        for i in range(3):
            self.b(r, "lesson", "editor", "--from", "team", "--principle", "one-place", "foreign " + str(i))
        self.assertEqual(self.notices(r), [])
        for i in range(3):
            self.record(r, "HiddenFixture " + str(i))
        (r / "nodes/app/never-name.txt").write_text("HiddenFixture\n")
        self.assertEqual(self.notices(r), [])
        path = folder / "lessons.md"
        path.write_text(path.read_text() + "".join(
            '- 2026-01-01 12:00 UTC · from app · tags {broken} · scope alice: x\n' for _ in range(6))
            + "".join('- 2026-01-01 12:00 UTC · from absent · scope alice: x\n' for _ in range(6)))
        self.assertEqual(self.notices(r), [])
        for i in range(3):
            self.record(r, "safe correction " + str(i))
        self.assertEqual(self.notices(r)[0]["count"], 3)
        self.assertNotIn("HiddenFixture", self.b(r, "due", "root", "--json"))
        # Changed tag policy is checked on every read, not just at capture time.
        (r / "nodes/app/never-name.txt").write_text("one-place\n")
        self.assertEqual(self.notices(r), [])

    def test_inactive_specialist_and_foreign_node_lessons_are_excluded(self):
        r, folder = self.fixture()
        for i in range(3):
            self.record(r, "correction " + str(i))
        role = folder / "role.md"
        original = role.read_text()
        for state in ("proposed", "rejected", "quarantined"):
            role.write_text(original.replace("status: active", "status: " + state))
            self.assertEqual(self.notices(r), [])
        role.write_text(original)
        self.assertEqual(len(self.notices(r)), 1)
        (r / "nodes/app/lessons.md").write_bytes((folder / "lessons.md").read_bytes())
        self.assertEqual(len(self.notices(r, "app")), 1)
        path = r / "nodes/app/lessons.md"
        path.write_text(path.read_text().replace("from app", "from root"))
        self.assertEqual(self.notices(r, "app"), [])

    def test_bad_receipt_is_actionable_without_reset_or_acknowledgement(self):
        r, folder = self.fixture()
        for i in range(3):
            self.record(r, "correction " + str(i))
        notice, = self.notices(r)
        path = folder / "synthesis.json"
        for broken in ("{broken", "[]", '{"version": 2, "seen": []}',
                       '{"version": 1, "seen": "x"}', '{"version": 1, "seen": [3]}'):
            path.write_text(broken)
            due = json.loads(self.b(r, "due", "root", "--json"))
            self.assertTrue(any(i["kind"] == "synthesis-state" for i in due))
            self.assertFalse(any(i["kind"] == "synthesis" for i in due))
            self.b(r, "due", "root", "--synthesis-seen", notice["set"], ok=False)
            self.assertEqual(path.read_text(), broken)

    def test_node_own_terms_and_same_named_specialists_stay_local(self):
        r, folder = self.fixture()
        self.b(r, "specialist", "new", "editor", "--at", "app", "--title", "Local editor")
        local = r / "nodes/app/specialists/editor"
        (local / "direction.md").write_bytes((folder / "direction.md").read_bytes())
        for i in range(3):
            self.b(r, "lesson", "editor", "--at", "root", "--from", "app", "--principle", "one-place", "root " + str(i))
            self.b(r, "lesson", "editor", "--at", "app", "--from", "app", "--principle", "one-place", "local " + str(i))
        root_notice, = self.notices(r)
        local_notice, = self.notices(r, "app")
        self.assertNotEqual(root_notice["set"], local_notice["set"])
        self.b(r, "due", "app", "--synthesis-seen", local_notice["set"])
        self.assertEqual(self.notices(r), [root_notice])
        self.assertFalse((folder / "synthesis.json").exists())
        (r / "nodes/app/never-name.txt").write_text("OwnFixture\n")
        for i in range(5):
            self.record(r, "OwnFixture " + str(i), "unclassified", target="app", node=True)
        node_notice, = self.notices(r, "app")
        self.assertEqual(node_notice["count"], 5)
        self.assertNotIn("OwnFixture", json.dumps(node_notice))

    def test_outward_receipt_link_refuses_writes_and_archived_node_has_no_due(self):
        r, folder = self.fixture()
        for i in range(3):
            self.record(r, "correction " + str(i))
        notice, = self.notices(r)
        outside = self.tmp / "receipt.json"
        outside.write_text('{"version": 1, "seen": []}\n')
        (folder / "synthesis.json").symlink_to(outside)
        self.b(r, "due", "root", "--synthesis-seen", notice["set"], ok=False)
        self.assertEqual(outside.read_text(), '{"version": 1, "seen": []}\n')
        (folder / "synthesis.json").unlink()
        self.b(r, "archive", "app")
        # Archived node due is empty, even if it has lessons.
        (r / "nodes/app/lessons.md").write_bytes((folder / "lessons.md").read_bytes())
        self.assertEqual(self.notices(r, "app"), [])

    def test_watcher_uses_same_set_and_writes_requires_instance_lock(self):
        r, _ = self.fixture()
        for i in range(3):
            self.record(r, "correction " + str(i))
        m = self.load_core()
        inst = m.Instance(r)
        n = inst.node("root")
        pending = [i for i in m.watch_items(inst, n) if i["kind"] == "synthesis"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending, [i for i in m.watch_items(inst, n) if i["kind"] == "synthesis"])
        notice, = self.notices(r)
        self.assertFalse(m.writes(argparse.Namespace(f=m.cmd_due, synthesis_seen=None)))
        self.assertTrue(m.writes(argparse.Namespace(f=m.cmd_due, synthesis_seen=notice["set"])))
        self.b(r, "due", "root", "--synthesis-seen", notice["set"])
        self.assertEqual([i for i in m.watch_items(m.Instance(r), n) if i["kind"] == "synthesis"], [])

    def test_unreadable_lessons_and_roles_do_not_stop_new_due_read_path(self):
        r, folder = self.fixture()
        for i in range(3):
            self.record(r, "correction " + str(i))
        m = self.load_core()
        inst = m.Instance(r)
        for filename in ("lessons.md", "role.md"):
            path = folder / filename
            original = path.read_bytes()
            path.write_bytes(original + b"\xff")
            # The existing facts/proposals scan already reads role metadata.
            # Exercise new role reads directly; lessons must survive the full CLI.
            due = (json.loads(self.b(r, "due", "root", "--json")) if filename == "lessons.md"
                   else m.synthesis_due(inst, inst.node("root")))
            self.assertTrue(any(i["kind"] == "synthesis-state" for i in due))
            self.assertFalse(any(i["kind"] == "synthesis" for i in due))
            watch = m.watch_items(inst, inst.node("root"))
            self.assertTrue(any(i["kind"] == "synthesis-state" for i in watch))
            # The new read path is local to the library, even for a damaged role.
            other = (json.loads(self.b(r, "due", "app", "--json")) if filename == "lessons.md"
                     else m.synthesis_due(inst, inst.node("app")))
            self.assertFalse(any(i["kind"].startswith("synthesis") for i in other))
            path.write_bytes(original)
        (r / "nodes/app/lessons.md").write_bytes(b"\xff")
        due = json.loads(self.b(r, "due", "app", "--json"))
        self.assertTrue(any(i["kind"] == "synthesis-state" for i in due))
        self.assertEqual(len(self.notices(r)), 1)

    def test_unreadable_source_policy_and_file_errors_fail_closed_per_target(self):
        r, folder = self.fixture()
        for i in range(3):
            self.record(r, "correction " + str(i))
        m = self.load_core()
        inst = m.Instance(r)
        n = inst.node("root")
        original = m.never_terms
        def failed_source(node):
            if node["id"] == "app":
                raise OSError("fixture unreadable source")
            return original(node)
        with mock.patch.object(m, "never_terms", side_effect=failed_source):
            due = m.synthesis_due(inst, n)
            self.assertTrue(any(i["kind"] == "synthesis-state" for i in due))
            self.assertFalse(any(i["kind"] == "synthesis" for i in due))
        with mock.patch.object(m, "parse_lessons", side_effect=OSError("fixture read failure")):
            due = m.synthesis_due(inst, n)
            self.assertTrue(any(i["kind"] == "synthesis-state" for i in due))
            self.assertFalse(any(i["kind"] == "synthesis" for i in due))
        # Existing non-files are damaged evidence, rather than missing lessons.
        path = folder / "lessons.md"
        path.unlink()
        path.mkdir()
        due = json.loads(self.b(r, "due", "root", "--json"))
        self.assertTrue(any(i["kind"] == "synthesis-state" for i in due))

    def test_failed_atomic_publication_leaves_set_pending(self):
        r, folder = self.fixture()
        for i in range(3):
            self.record(r, "correction " + str(i))
        m = self.load_core()
        inst = m.Instance(r)
        n = inst.node("root")
        notice, = self.notices(r)
        with mock.patch.object(m, "atomic_write", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                m.synthesis_seen(inst, n, notice["set"])
        self.assertFalse((folder / "synthesis.json").exists())
        self.assertEqual(self.notices(r), [notice])
