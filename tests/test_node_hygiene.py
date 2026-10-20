"""Made-up node hygiene scenarios, including exact threshold and notification controls."""
import datetime
import importlib.machinery
import importlib.util
import json
from unittest import mock

from test_brain import Scenario, CORE


class NodeHygiene(Scenario):
    def setUp(self):
        super().setUp()
        loader = importlib.machinery.SourceFileLoader('brain_hygiene', str(CORE / 'bin/brain'))
        self.m = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.m)

    def node(self):
        r = self.init('maple')
        self.b(r, 'new', 'app', '--title', 'App')
        inst = self.m.Instance(r)
        return r, inst, inst.node('app')

    def block(self, n, since):
        text = ('Choose a color — waiting on owner; clears when chosen; since: ' + since +
                '; recommendation: blue; default: keep gray')
        with (n['dir'] / 'status.md').open('a') as f:
            f.write('\n## Blocked / needs the operator\n\n- ' + text + '\n')
        return text

    def test_facts_required_refresh_preserves_judgement(self):
        r, inst, n = self.node()
        st = n['dir'] / 'status.md'
        self.assertIn(self.m.FACTS_START, st.read_text())
        judgement = 'Updated: 2020-01-01 00:00 +00:00\n\n# App\n\n## Now\n\n- Exact judgement.\n'
        st.write_text(judgement)
        self.assertIn('missing or malformed generated facts block', self.b(r, 'check', ok=False))
        self.b(r, 'status', 'app', '--refresh')
        self.assertIn('✓', self.b(r, 'check'))
        self.assertEqual(self.m.FACTS_RE.sub('', st.read_text()).replace('\n\n\n\n', '\n\n'), judgement)
        st.write_text(st.read_text() + self.m.FACTS_START)
        self.assertIn('malformed', self.b(r, 'check', ok=False))

    def test_now_limit_counts_wrapped_lines_but_not_history_or_comments(self):
        r, inst, n = self.node()
        st = n['dir'] / 'status.md'
        base = self.m.FACTS_RE.search(st.read_text()).group()
        st.write_text(base + '\n## Now\n' + '- current\n' * 30 + '\n<!-- comment -->\n## Recent changes\n' + '- old\n' * 100)
        self.assertNotIn('Now has', self.b(r, 'check'))
        st.write_text(st.read_text().replace('## Recent changes', '  wrapped\n## Recent changes'))
        self.assertIn('Now has 31 nonblank lines (limit 30); move history', self.b(r, 'check'))

    def test_blocked_exact_threshold_escalates_once_without_clock_renotification(self):
        r, inst, n = self.node()
        since = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        text = self.block(n, since.isoformat())
        with mock.patch.object(inst, 'now_dt', return_value=since + datetime.timedelta(hours=48)):
            b = self.m.blocked_entries(inst, n)[0]
            self.assertFalse(b['escalated'])
            self.assertEqual(b['age_hours'], 48)
            before = self.m.inbox_items(inst, [n], 'app')[0]
        with mock.patch.object(inst, 'now_dt', return_value=since + datetime.timedelta(hours=49)):
            due = [i for i in self.m.due_items(inst, n) if i['kind'] == 'blocked-overdue']
            self.assertEqual(len(due), 1)
            self.assertEqual(due[0]['age_hours'], 49)
            self.assertEqual(due[0]['recommendation'], 'blue')
            self.assertEqual(due[0]['default'], 'keep gray')
            after = self.m.inbox_items(inst, [n], 'app')[0]
            self.assertNotEqual(before[0]['id'], after[0]['id'])
        saved = r / '.brain/inbox-seen.json'
        saved.write_text(json.dumps({'version': 2, 'reporters': {'app': {
            i['id']: {'reported_at': since.isoformat(), 'first_seen': since.isoformat()} for i in after}}}))
        with mock.patch.object(inst, 'now_dt', return_value=since + datetime.timedelta(hours=50)):
            later = self.m.inbox_items(inst, [n], 'app')[0]
            self.assertEqual(after[0]['id'], later[0]['id'])
            self.assertTrue(later[0]['reported'])
        attention = json.loads(self.b(r, 'ui', '--json'))['attention']
        item = next(i for i in attention if i['kind'] == 'blocked-overdue')
        self.assertTrue(item['escalated'])
        self.assertEqual(item['text'], text)
        self.assertIn('blocked-overdue', self.b(r, 'due', 'app'))

    def test_legacy_blocked_age_and_fixable_format_warning(self):
        r, inst, n = self.node()
        st = n['dir'] / 'status.md'
        with st.open('a') as f:
            f.write('\n## Blocked\n- Need a choice\n')
        self.git('add', '.', cwd=r)
        self.git('commit', '-qm', 'record need', cwd=r)
        intro = inst.now_dt()
        with mock.patch.object(inst, 'now_dt', return_value=intro + datetime.timedelta(hours=49)):
            self.assertTrue(self.m.blocked_entries(inst, n)[0]['escalated'])
        self.assertIn('missing valid since:', self.b(r, 'check'))
        self.assertIn("doesn't say what clears it", self.b(r, 'check'))
        # Empty placeholder bullets never become decisions.
        st.write_text(self.m.FACTS_RE.search(st.read_text()).group() + '\n## Blocked\n- None.\n')
        self.assertEqual(self.m.blocked_entries(inst, n), [])

    def test_code_setup_warns_without_stranding_jobs_and_accepts_bound_tick(self):
        r, inst, n = self.node()
        home = self.repo(self.tmp / 'source')
        self.b(r, 'set', 'app', 'home=' + str(home))
        out = self.b(r, 'wt', 'add', 'app', 'job-first')
        self.assertIn('needs verify=<command>', out)
        self.assertIn('needs a coordinator tick', out)
        self.assertIn('worktree:', out)
        self.assertIn('needs verify=<command>', self.b(r, 'check'))
        self.b(r, 'set', 'app', 'verify=python3 -c "pass"')
        self.b(r, 'schedule', 'add', 'app-tick', '--when', 'every 15 minutes', '--node', 'app', '--brief', 'Run due and pending')
        self.assertIn('needs a coordinator tick', self.b(r, 'check'))
        self.b(r, 'schedule', 'bind', 'app-tick', 'fixture-host-id')
        self.assertNotIn('needs a coordinator tick', self.b(r, 'check'))
        self.assertNotIn('needs verify=<command>', self.b(r, 'check'))

    def test_dead_note_paths_node_and_home_urls_globs_commands(self):
        r, inst, n = self.node()
        home = self.repo(self.tmp / 'source', {'present.py': '# file\n'})
        self.b(r, 'set', 'app', 'home=' + str(home))
        (n['dir'] / 'local').mkdir()
        (n['dir'] / 'brief.md').write_text('`present.py` `local/` `gone/file.py:12`\n'
            '[lost](missing.md#heading) [web](https://example.test/x)\n'
            '`*.py` `src/{a,b}.py` `brain check` `https://example.test`\n')
        (n['dir'] / 'knowledge.md').write_text('[gone folder](absent/)\n')
        out = self.b(r, 'check')
        self.assertIn('dead path gone/file.py:12', out)
        self.assertIn('dead path missing.md#heading', out)
        self.assertIn('dead path absent/', out)
        self.assertNotIn('dead path present.py', out)
        self.assertNotIn('dead path local/', out)
        self.assertNotIn('dead path https:', out)
        self.assertNotIn('dead path *.py', out)
        self.assertNotIn('dead path brain check', out)

    def test_routing_rules_keep_sync_and_design_owned(self):
        role = (CORE / 'roles/coordinator.md').read_text()
        home = (CORE / 'skills/home-job/SKILL.md').read_text()
        tick = (CORE / 'adapters/hosts/t3.md').read_text()
        sync = (CORE / 'skills/node-sync/SKILL.md').read_text()
        self.assertIn('root never runs child sync', role)
        self.assertIn('ui-designer specialist (§0)', role)
        self.assertIn('before any UI code', home)
        self.assertIn('accepted design', home)
        self.assertIn('root never runs child sync', tick)
        self.assertIn('only for its own node', sync)
