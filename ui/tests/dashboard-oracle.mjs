// Pure display oracle shared by focused negative controls and the real browser audit.
// Shaping functions supply the UI's documented display transformations, never data.
export function createDashboardOracle(model) {
  const normalize = value => String(value ?? '').replace(/\s+/g, ' ').trim();
  const check = (value, message) => { if (!value) throw Error(message); };
  const display = (value, state) => normalize(model.humanText(model.readable(value, state.brain?.tz)).replace(/`([^`\n]+)`/g, '$1'));
  const project = (state, item) => state.nodes.find(node => node.id === item.node)?.title || 'Workspace';
  function request(state, item) {
    const body = String(item.text || '').replace(/^(?:inbox:\s*)?\d{4}-\d{2}-\d{2}[^\n]*? from [^:]+:\s*(?:question:\s*)?/i, '');
    const row = { project: display(project(state, item), state),
      sender: normalize(model.sender(item) + ' · ' + model.time(item.first_seen, state.brain?.tz)),
      body: display(body, state) };
    if (typeof item.recommendation === 'string' && item.recommendation)
      row.recommendation = 'Recommended: ' + display(item.recommendation, state);
    return row;
  }
  function step(state, item) {
    const parts = model.dueParts(item);
    const row = { project: display(project(state, item), state), label: display(parts.label, state), body: display(parts.body, state) };
    if (parts.title) row.job = display(parts.title, state);
    return row;
  }
  const text = (root, selector) => normalize(root.querySelector(selector)?.textContent);
  function renderedRequest(root) {
    const row = { project: text(root, '.row-line > strong'), sender: text(root, '.row-line > small'), body: text(root, '.row-text') };
    if (root.querySelector('.row-note')) row.recommendation = text(root, '.row-note');
    return row;
  }
  function renderedStep(root) {
    const jobContext = root.querySelector('.row-line > small');
    const row = { project: text(root, jobContext ? '.row-line > small' : '.row-line > strong'),
      label: text(root, '.row-line > .status'), body: text(root, '.row-text') };
    if (jobContext) row.job = text(root, '.row-line > strong');
    return row;
  }
  function signature(row) {
    check(row && typeof row === 'object' && !Array.isArray(row), 'Missing display record');
    for (const key of ['project', 'body']) check(normalize(row[key]), 'Blank display ' + key);
    const fields = Object.keys(row).sort().map(key => {
      check(typeof row[key] === 'string' && normalize(row[key]), 'Blank display ' + key);
      return [key, normalize(row[key])];
    });
    return JSON.stringify(fields);
  }
  function compareRows(expected, actual) {
    check(expected.length === actual.length, `Rendered ${actual.length} rows; backend has ${expected.length}`);
    const inventory = rows => {
      const found = new Set();
      for (const row of rows) {
        const key = signature(row);
        check(!found.has(key), 'Duplicate or ambiguous display record');
        found.add(key);
      }
      return found;
    };
    const wanted = inventory(expected), seen = inventory(actual);
    for (const key of wanted) check(seen.has(key), 'Missing or substituted backend display record: ' + key);
    return seen.size;
  }
  function overview(state) {
    const needs = model.needsView(state), now = model.nowOf(state);
    const merged = state.nodes.flatMap(node => node.jobs?.rows || []).filter(row =>
      /merged/i.test(row.Outcome || '') && model.stamp(row.Ended) <= now && model.stamp(row.Ended) > now - 86400000);
    return [
      { label: 'Needs you', value: String(needs.inbox.length + needs.due.length) },
      { label: 'Merges today', value: String(merged.length) },
      { label: 'Threads live', value: String(model.threads(state.nodes).length) },
      { label: 'Ready to merge', value: String(needs.due.filter(row => row.ready).length) },
      { label: 'Verify queue', value: String(state.machine?.queued ?? '—'),
        note: state.machine?.verifying != null
          ? `${state.machine.verifying} of ${state.machine.verify_limit ?? '?'} verifying` : 'queue not readable' },
    ];
  }
  function renderedOverview(root) {
    return [...root.querySelectorAll('[data-kpi]')].map(kpi => {
      const row = { label: text(kpi, '.kpi-label'), value: text(kpi, '.kpi-value') };
      if (row.label === 'Verify queue') row.note = text(kpi, '.kpi-note');
      return row;
    });
  }
  function compareOverview(state, actual) {
    const expected = overview(state);
    check(actual.length === expected.length, 'Overview KPI count differs from backend');
    const seen = new Map();
    for (const row of actual) {
      check(normalize(row.label) && normalize(row.value), 'Blank overview KPI');
      check(!seen.has(normalize(row.label)), 'Duplicate overview KPI label');
      seen.set(normalize(row.label), row);
    }
    for (const row of expected) {
      const found = seen.get(row.label);
      check(found && normalize(found.value) === row.value, 'Wrong overview KPI: ' + row.label);
      if (row.note) check(normalize(found.note) === row.note, 'Wrong overview KPI availability: ' + row.label);
    }
    return actual.length;
  }
  return { request, step, renderedRequest, renderedStep, compareRows, overview, renderedOverview, compareOverview };
}
