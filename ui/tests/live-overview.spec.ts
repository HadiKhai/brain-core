import { test, expect } from '@playwright/test';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';

test('opt-in live keyed overview fitness (GET only, no private artifacts)', async () => {
  test.skip(!process.env.BRAIN_UI_LIVE_URL, 'Set BRAIN_UI_LIVE_URL privately to opt in.');
  test.setTimeout(150_000);
  let counts: Record<string, unknown> = { failed: true };
  try {
    const { stdout } = await promisify(execFile)(process.execPath, [fileURLToPath(new URL('./live-overview-probe.mjs', import.meta.url))], {
      env: { ...process.env, DEBUG: '', PWDEBUG: '' }, timeout: 140_000, maxBuffer: 4096,
    });
    counts = JSON.parse(stdout);
  } catch { /* Report scalar failure only; child errors may contain the private URL. */ }
  console.log('Live overview fitness counts:', JSON.stringify(counts));
  expect(counts.failed).toBeUndefined();
  expect(counts.validTarget).toBe(true);
  expect(counts.overview).toBe(true);
  expect(counts.kpis).toBe(5);
  expect(counts.nonempty).toBe(true);
  expect(counts.retained).toBe(true);
  expect(counts.fatal).toBe(0);
  expect(counts.consoleErrors).toBe(0);
  expect(counts.pageErrors).toBe(0);
  expect(counts.forbiddenRequests).toBe(0);
});
