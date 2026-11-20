import { expect, test, type Page } from '@playwright/test';
import { measurementAssertion, reportVisualWarnings } from './visual-report.mjs';

export const reportMeasurements = (page: Page, issues: unknown[]) => reportVisualWarnings(page, test.info(), issues);

// Explicit opt-in at each measurement assertion. Ordinary expect/click/focus stay hard.
export function visualExpect(page: Page, actual: unknown, message?: string): ReturnType<typeof expect> {
  const wrap = (assertion: any): any => new Proxy(assertion, {
    get(target, key) {
      if (key === 'not') return wrap(target.not);
      return (...args: unknown[]) => measurementAssertion(
        () => target[key](...args),
        (issue: unknown) => reportMeasurements(page, [issue]),
      );
    },
  });
  return wrap(expect(actual, message));
}

export async function fitnessAudit(page: Page, source: string): Promise<any> {
  const result = await page.evaluate(source) as any;
  await reportMeasurements(page, result.warnings || []);
  return result;
}
