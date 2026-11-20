import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, mkdtempSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import vm from 'node:vm';
import { measurementAssertion, reportVisualWarnings } from './visual-report.mjs';
import VisualReporter from './visual-reporter.mjs';

const source = readFileSync(new URL('../../tests/ui-fitness.js', import.meta.url), 'utf8');
// Small DOM double executes the actual browser audit without installing browser dependencies.
function runAudit({ miss = false, unlabelled = false, cut = false, tooltip = false } = {}) {
  const element = (left = 16, right = 304) => ({
    clientLeft: 0, clientWidth: right-left, clientHeight: 40, scrollWidth: right-left, scrollHeight: 40,
    textContent: 'Synthetic status', type: 'text', placeholder: 'Value', childNodes: [],
    closest: () => null, querySelector: () => null, querySelectorAll: () => [], matches: () => false,
    getAttribute: () => null, getClientRects: () => [1],
    getBoundingClientRect: () => ({left, right, top: 10, bottom: 50, x:left, y:10, width:right-left, height:40}),
  });
  const surface = element(), row = element(miss ? 2 : 16), footer = element(miss ? 30 : 16);
  row.closest = selector => selector === '.surface, .page-section' ? surface : null;
  surface.querySelectorAll = selector => selector === '.rows > .row' ? [row] : selector.startsWith(':scope > .list-footer') ? [footer] : [];
  const scroller = element(0,320), header = element(), active = element(), status = element();
  if (cut) status.scrollHeight = 80;
  if (tooltip) status.closest = selector => selector === '[data-slot=tooltip-trigger]' ? {} : null;
  const document = {
    documentElement: {scrollWidth:320, classList: { contains: () => false }},
    querySelectorAll: selector => selector === '.surface, .page-section' ? [surface] : selector === '.status' ? [status] : selector === 'input:not([readonly]),textarea' && unlabelled ? [element()] : [],
    querySelector: selector => ({'.inset-scroll': scroller, '#workspace-content': scroller, '.topbar':header, '.page-header':header, '[data-page]:not([hidden])':active})[selector] || null,
    createElement: () => ({getContext: () => ({})}),
  };
  return vm.runInNewContext(source, { document, innerWidth:320, innerHeight:640,
    getComputedStyle: () => ({paddingLeft:'0', paddingRight:'0', visibility:'visible', fontSize:'16px'}) });
}

test('real audit logic: 14px inset misses report values and the run passes', async () => {
  assert.equal(runAudit().warnings.length, 0);
  const result = runAudit({miss:true});
  assert.equal(result.warnings.length, 2);
  assert.equal(result.warnings[0].actual, 2);
  assert.equal(result.warnings[0].expected, 16);
  const info = {annotations:[], attachments:[], async attach(name, data) { this.attachments.push({name,...data}); }};
  const page = {url: () => 'http://synthetic.invalid/inbox', viewportSize: () => ({width:320,height:640}),
    screenshot: async () => { throw Error('Logic-only proof: browser not installed in worktree'); }};
  await reportVisualWarnings(page, info, result.warnings);
  assert.equal(info.annotations[0].type, 'visual-warning');
  assert.match(info.attachments[0].body, /section content inset/);
  assert.equal(info.annotations[1].type, 'visual-evidence-warning');
  const folder = mkdtempSync(fileURLToPath(new URL('../../docs/visual-proof-', import.meta.url)));
  const saved = {report:process.env.VISUAL_WARNING_REPORT, summary:process.env.GITHUB_STEP_SUMMARY};
  try {
    process.env.VISUAL_WARNING_REPORT = join(folder, 'warnings.json');
    process.env.GITHUB_STEP_SUMMARY = join(folder, 'summary.md');
    const reporter = new VisualReporter();
    reporter.onTestEnd({titlePath:()=>['alignment logic mutation'], location:{file:'ui/tests/alignment.spec.ts',line:54}}, info);
    reporter.onEnd();
    assert.match(readFileSync(process.env.GITHUB_STEP_SUMMARY, 'utf8'), /Visual measurement warnings: 2/);
    assert.equal(JSON.parse(JSON.parse(readFileSync(process.env.VISUAL_WARNING_REPORT, 'utf8'))[0].description).issues[0].actual, 2);
  } finally {
    for (const [key,value] of Object.entries({VISUAL_WARNING_REPORT:saved.report,GITHUB_STEP_SUMMARY:saved.summary})) value === undefined ? delete process.env[key] : process.env[key] = value;
    rmSync(folder, {recursive:true});
  }
});

test('audit keeps missing labels and cut text without hover hard', () => {
  assert.throws(() => runAudit({unlabelled:true}), /missingFieldLabels/);
  assert.throws(() => runAudit({cut:true}), /no full text on hover/);
  assert.equal(runAudit({cut:true,tooltip:true}).warnings[0].kind, 'text-overflow');
});

test('measurement adapter warns, keeps evaluating, and propagates non-assertion failures', async () => {
  const warnings = [];
  const error = Object.assign(Error('14px versus 0px'), {matcherResult:{actual:14,expected:0}});
  await measurementAssertion(() => { throw error; }, issue => warnings.push(issue));
  assert.deepEqual(warnings, [{detail:'14px versus 0px',actual:14,expected:0}]);
  await measurementAssertion(() => {}, () => assert.fail('clean checks never warn'));
  await assert.rejects(measurementAssertion(() => { throw Error('control unreachable'); }, () => {}), /control unreachable/);
});

test('warning screenshots are attached when the browser supplies them', async () => {
  const attachments = [];
  const info = {annotations:[], attach:async (name, data) => attachments.push({name,...data})};
  const png = Buffer.from('synthetic screenshot bytes');
  await reportVisualWarnings({url:()=>'/synthetic',viewportSize:()=>null,screenshot:async()=>png}, info, [{actual:14,expected:0}]);
  assert.equal(attachments[1].contentType, 'image/png');
  assert.equal(attachments[1].body, png);
});
