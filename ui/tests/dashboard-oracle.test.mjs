import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { createDashboardOracle } from './dashboard-oracle.mjs';

const require = createRequire(path.resolve(process.env.DASHBOARD_TEST_TOOLS || 'ui', 'package.json'));
const ts = require('typescript');
const source = ts.transpile(readFileSync(new URL('../src/model.ts', import.meta.url), 'utf8'),
  { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 });
const model = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const oracle = createDashboardOracle(model);
const state = { brain: { now: '2026-01-01T12:00:00Z', tz: 'UTC' }, machine: {},
  nodes: [{ id: 'demo', title: 'Notebook', jobs: { rows: [], open: [] }, work: [], worktrees: [] }],
  inbox: [{ id: 'request', node: 'demo', kind: 'inbox', text: 'Choose a cover?' }],
  due: [{ id: 'step', node: 'demo', kind: 'handback', branch: 'job/cover', text: 'job/cover: review the cover', ready: true }] };
const request = oracle.request(state, state.inbox[0]);
const step = oracle.step(state, state.due[0]);

test('request and step projections retain nonempty body and project/job context', () => {
  assert.equal(request.project, 'Notebook');
  assert.equal(request.body, 'Choose a cover?');
  assert.deepEqual(step, { project: 'Notebook', label: 'Ready to merge', body: 'review the cover', job: 'Cover' });
  assert.equal(oracle.compareRows([request, step], [step, request]), 2);
});
for (const [name, row] of [['request', request], ['step', step]]) {
  test(`blank ${name} body must be rejected`, () => {
    assert.throws(() => oracle.compareRows([row], [{ ...row, body: '' }]), /Blank/);
    assert.throws(() => oracle.compareRows([{ ...row, body: '' }], [row]), /Blank/);
  });
  test(`duplicate or substituted ${name} must be rejected`, () => {
    const other = { ...row, body: row.body + ' another decision' };
    assert.throws(() => oracle.compareRows([row, other], [row, row]), /Duplicate/);
    assert.throws(() => oracle.compareRows([row], [{ ...row, body: 'Unrelated row' }]), /substituted/);
    assert.throws(() => oracle.compareRows([row], [{ ...row, project: 'Other notebook' }]), /substituted/);
  });
}
test('substituting only the job context must be rejected', () => {
  assert.throws(() => oracle.compareRows([step], [{ ...step, job: 'Another job' }]), /substituted/);
});
test('all overview labels and values must agree with the backend', () => {
  const rows = oracle.overview(state);
  assert.equal(oracle.compareOverview(state, rows), 5);
  assert.deepEqual(rows.map(row => [row.label, row.value]), [
    ['Needs you', '2'], ['Merges today', '0'], ['Threads live', '0'], ['Ready to merge', '1'], ['Verify queue', '—']]);
  for (let i = 0; i < rows.length; i++) {
    const changed = structuredClone(rows); changed[i].value = '999';
    assert.throws(() => oracle.compareOverview(state, changed), /Wrong overview KPI/);
  }
  assert.throws(() => oracle.compareOverview(state, [rows[0], rows[0], ...rows.slice(2)]), /Duplicate/);
});
test('unavailable queue values stay unavailable instead of becoming zero', () => {
  const rows = oracle.overview(state);
  assert.deepEqual(rows[4], { label: 'Verify queue', value: '—', note: 'queue not readable' });
  assert.throws(() => oracle.compareOverview(state, [...rows.slice(0, 4), { ...rows[4], value: '0' }]), /Wrong overview KPI/);
  assert.throws(() => oracle.compareOverview(state, [...rows.slice(0, 4), { ...rows[4], note: '0 of 1 verifying' }]), /availability/);
  const readable = { ...state, machine: { queued: 0, verifying: 1, verify_limit: 2 } };
  assert.deepEqual(oracle.overview(readable)[4], { label: 'Verify queue', value: '0', note: '1 of 2 verifying' });
});
