// Measurement evidence is advisory. Functional assertions use the normal test API.
export async function reportVisualWarnings(page, info, issues) {
  if (!issues.length) return;
  const name = `visual-warning-${info.annotations.filter(a => a.type === 'visual-warning').length + 1}`;
  const evidence = { url: page.url(), viewport: page.viewportSize(), issues };
  info.annotations.push({ type: 'visual-warning', description: JSON.stringify(evidence) });
  await info.attach(`${name}-values`, { body: JSON.stringify(evidence, null, 2), contentType: 'application/json' });
  try {
    await info.attach(`${name}-screenshot`, { body: await page.screenshot({ animations: 'disabled', timeout: 5000 }), contentType: 'image/png' });
  } catch (error) {
    // An unavailable screenshot must not turn a measurement into a failing gate.
    info.annotations.push({ type: 'visual-evidence-warning', description: `Screenshot unavailable: ${error.message}` });
  }
  console.warn(`VISUAL WARNING ${JSON.stringify(evidence)}`);
}

export async function measurementAssertion(assertion, report) {
  try { await assertion(); }
  catch (error) {
    // Never swallow page errors, locator timeouts, or other functional failures.
    if (!error.matcherResult) throw error;
    await report({ detail: error.message, actual: error.matcherResult.actual, expected: error.matcherResult.expected });
  }
}
