"""Product adapters are fabricated; no preview process or provider is contacted."""
import json
from unittest import mock

from test_handback_queue import HandbackLifecycle
from scenario import Scenario


class PreviewUpgrade(Scenario):
    fixture = HandbackLifecycle.fixture
    def preview(self):
        m, inst, n, _ = self.fixture()
        inst.local_of('root')['preview_upgrade'] = dict(version=1, enabled=True, adapter='fixture',
            protocol='fixture-v1', resource='fixture-preview', backup='snapshot-v1', migration='schema-v1',
            restart='restart-v1', acceptance='http-v1', rollback='restore-v1')
        inst.save_local()
        n = inst.node('root')
        self.calls = []
        class Adapter:
            protocol = 'fixture-v1'
            def step(adapter, step, context):
                self.calls.append((step, dict(context)))
                return dict(ok=True, receipt='fixture-' + step)
        return m, inst, n, Adapter()

    def test_protocol_order_and_durable_per_head_idempotence(self):
        m, inst, n, adapter = self.preview()
        with mock.patch.object(m, 'preview_landed_head', return_value='a'*40):
            result = m.upgrade_preview(inst, n, 'a'*40, adapters={'fixture': adapter})
            self.assertEqual(result['state'], 'succeeded')
            self.assertEqual([x[0] for x in self.calls], ['backup', 'migration', 'restart', 'acceptance'])
            self.assertEqual(m.upgrade_preview(inst, n, 'a'*40, adapters={'fixture': adapter}), result)
            self.assertEqual(len(self.calls), 4)
            self.assertEqual(self.calls[1][1]['backup_receipt'], 'fixture-backup')

    def test_failed_acceptance_rolls_back_and_never_claims_success(self):
        m, inst, n, adapter = self.preview()
        original = adapter.step
        def step(name, context):
            result = original(name, context)
            return {**result, 'ok': name != 'acceptance'}
        adapter.step = step
        with mock.patch.object(m, 'preview_landed_head', return_value='a'*40):
            result = m.upgrade_preview(inst, n, 'a'*40, adapters={'fixture': adapter})
            self.assertEqual(result['state'], 'failed')
            self.assertTrue(result['rolled_back'])
            self.assertEqual(self.calls[-1][0], 'rollback')
            m.upgrade_preview(inst, n, 'a'*40, adapters={'fixture': adapter})
            self.assertEqual(len(self.calls), 5)

    def test_unknown_adapter_incomplete_protocol_and_stale_head_do_no_work(self):
        m, inst, n, adapter = self.preview()
        with mock.patch.object(m, 'preview_landed_head', return_value='b'*40):
            self.assertEqual(m.upgrade_preview(inst, n, 'a'*40, adapters={'fixture':adapter})['state'], 'pending')
        with mock.patch.object(m, 'preview_landed_head', return_value='a'*40):
            self.assertEqual(m.upgrade_preview(inst, n, 'a'*40, adapters={})['state'], 'pending')
            inst.local_of('root')['preview_upgrade'].pop('rollback')
            inst.save_local()
            self.assertEqual(m.upgrade_preview(inst, inst.node('root'), 'a'*40,
                                             adapters={'fixture': adapter})['state'], 'pending')
        self.assertEqual(self.calls, [])

    def test_crash_retains_unknown_step_and_blocks_other_heads(self):
        m, inst, n, adapter = self.preview()
        original = adapter.step
        def crash(name, context):
            if name == 'migration':
                raise KeyboardInterrupt('fixture crash')
            return original(name, context)
        adapter.step = crash
        with mock.patch.object(m, 'preview_landed_head', return_value='a'*40):
            with self.assertRaises(KeyboardInterrupt):
                m.upgrade_preview(inst, n, 'a'*40, adapters={'fixture':adapter})
            result = m.upgrade_preview(inst, n, 'a'*40, adapters={'fixture':adapter})
            self.assertEqual(result['state'], 'pending')
            self.assertEqual(result['active_step'], 'migration')
        with mock.patch.object(m, 'preview_landed_head', return_value='b'*40):
            self.assertEqual(m.upgrade_preview(inst, n, 'b'*40, adapters={'fixture':adapter})['state'], 'pending')
        self.assertEqual([x[0] for x in self.calls], ['backup'])

    def test_one_resource_owner_and_configuration_drift_are_fenced(self):
        m, inst, n, adapter = self.preview()
        original = adapter.step
        def concurrent(name, context):
            if name == 'backup':
                other = m.upgrade_preview(inst, n, 'a'*40, adapters={'fixture':adapter})
                self.assertEqual(other['state'], 'pending')
                inst.local_of('root')['preview_upgrade']['resource'] = 'changed'
                inst.save_local()
            return original(name, context)
        adapter.step = concurrent
        with mock.patch.object(m, 'preview_landed_head', return_value='a'*40):
            result = m.upgrade_preview(inst, n, 'a'*40, adapters={'fixture':adapter})
            self.assertEqual(result['state'], 'pending')
            self.assertEqual([x[0] for x in self.calls], ['backup'])

    def test_landing_hooks_run_after_publication_and_keep_settlement_separate(self):
        m, inst, n, adapter = self.preview()
        path = n['dir'] / 'fixture-train.json'
        data = dict(tip='a'*40, accepted=[], publication_succeeded=False, settle_requested=False)
        path.write_text(json.dumps(data))
        with mock.patch.object(m, 'landed_preview', return_value=dict(state='pending', head='a'*40)) as upgrade:
            m.post_publication(inst, n, path, data)
            upgrade.assert_not_called()
            data['publication_succeeded'] = True
            path.write_text(json.dumps(data))
            self.assertIsNone(m.post_publication(inst, n, path, data))
            upgrade.assert_called_once_with(inst, n, 'a'*40)
            self.assertEqual(json.loads(path.read_text())['preview_upgrade']['state'], 'pending')
            upgrade.reset_mock()
            m.job_preview_after_landing(inst, n, dict(Outcome='closed'))
            upgrade.assert_not_called()
            with mock.patch.object(m, 'preview_landed_head', return_value='a'*40):
                m.job_preview_after_landing(inst, n, dict(Outcome='merged'))
            upgrade.assert_called_once_with(inst, n, 'a'*40)

    def test_settings_are_local_and_require_all_protocol_steps(self):
        m, inst, n, _ = self.preview()
        config = dict(n['local']['preview_upgrade'])
        messages = []
        fm, body = m.prepare_settings(inst, n, ['preview_upgrade=' + json.dumps(config)], messages)
        self.assertNotIn('preview_upgrade', fm)
        self.assertEqual(inst.local_of('root')['preview_upgrade'], config)
        config.pop('backup')
        with self.assertRaises(SystemExit):
            m.prepare_settings(inst, n, ['preview_upgrade=' + json.dumps(config)], [])

    def test_foreign_host_receipts_never_authorize_preview_work(self):
        m, inst, n, adapter = self.preview()
        with mock.patch.object(m, 'preview_landed_head', return_value='a'*40):
            m.upgrade_preview(inst, n, 'a'*40, adapters={'fixture': adapter})
            path = m.preview_folder(n) / ('a'*40 + '.json')
            data = json.loads(path.read_text())
            data['host'] = 'fixture-boot-b'
            path.write_text(json.dumps(data))
            before = path.read_bytes()
            result = m.upgrade_preview(inst, n, 'a'*40, adapters={'fixture': adapter})
            self.assertEqual(result['state'], 'pending')
            self.assertEqual(path.read_bytes(), before)
        with mock.patch.object(m, 'preview_landed_head', return_value='b'*40):
            result = m.upgrade_preview(inst, n, 'b'*40, adapters={'fixture': adapter})
            self.assertEqual(result['state'], 'pending')
        self.assertEqual(len(self.calls), 4)

    def test_incomplete_terminal_receipt_cannot_unlock_next_head(self):
        m, inst, n, adapter = self.preview()
        with mock.patch.object(m, 'preview_landed_head', return_value='a'*40):
            m.upgrade_preview(inst, n, 'a'*40, adapters={'fixture': adapter})
        path = m.preview_folder(n) / ('a'*40 + '.json')
        data = json.loads(path.read_text())
        data['steps'] = []
        path.write_text(json.dumps(data))
        with mock.patch.object(m, 'preview_landed_head', return_value='b'*40):
            self.assertEqual(m.upgrade_preview(inst, n, 'b'*40,
                adapters={'fixture': adapter})['state'], 'pending')
        self.assertEqual(len(self.calls), 4)

    def test_unknown_host_does_not_create_state_or_call_product(self):
        m, inst, n, adapter = self.preview()
        with mock.patch.object(m, 'lifecycle_host_identity', return_value=None):
            self.assertEqual(m.upgrade_preview(inst, n, 'a'*40,
                adapters={'fixture': adapter})['state'], 'pending')
        self.assertEqual(self.calls, [])
        self.assertFalse(m.preview_folder(n).exists())

    def test_historical_success_cannot_hide_another_heads_unknown_effects(self):
        m, inst, n, adapter = self.preview()
        with mock.patch.object(m, 'preview_landed_head', return_value='a'*40):
            self.assertEqual(m.upgrade_preview(inst, n, 'a'*40,
                adapters={'fixture': adapter})['state'], 'succeeded')
        original = adapter.step
        def unknown(name, context):
            if name == 'migration':
                raise RuntimeError('fixture uncertain migration')
            return original(name, context)
        adapter.step = unknown
        with mock.patch.object(m, 'preview_landed_head', return_value='b'*40):
            self.assertEqual(m.upgrade_preview(inst, n, 'b'*40,
                adapters={'fixture': adapter})['state'], 'pending')
        calls = list(self.calls)
        with mock.patch.object(m, 'preview_landed_head', return_value='a'*40):
            self.assertEqual(m.upgrade_preview(inst, n, 'a'*40,
                adapters={'fixture': adapter})['state'], 'pending')
        self.assertEqual(self.calls, calls)


del HandbackLifecycle
