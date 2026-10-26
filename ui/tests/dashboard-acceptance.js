// Evaluate this function in a browser on the actual authenticated brain ui URL.
// It never injects state, intercepts requests, or changes backend records.
async function dashboardAcceptance(mode = 'views', model, oracle, deliveredReading) {
  const check = (value, message) => { if (!value) throw Error(message); };
  const normalize = text => String(text ?? '').replace(/\s+/g, ' ').trim();
  const tick = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const settle = () => new Promise(resolve => setTimeout(resolve, 20));
  const wait = async predicate => {
    const deadline = performance.now() + 15000;
    while (!predicate()) {
      check(performance.now() < deadline, 'DOM reading did not arrive');
      await new Promise(resolve => setTimeout(resolve, 5));
    }
  };
  check(location.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(location.hostname), 'Use a local serving URL');
  check(!window.__STATE__, 'Injected snapshots cannot qualify serving acceptance');
  const key = new URLSearchParams(location.search).get('k');
  check(key, 'Use the authenticated app URL');
  check(model && oracle, 'Load the source display model and strict oracle');
  let state;
  if (mode === 'first-reading') {
    check(deliveredReading?.brain && Array.isArray(deliveredReading.nodes), 'Observe the app HTTP reading before measuring paint');
    state = deliveredReading;
  } else {
    const response = await fetch('/api/state?k=' + encodeURIComponent(key));
    check(response.ok, 'Real backend state unavailable');
    state = await response.json();
  }
  if (mode === 'first-reading') {
    await wait(() => {
      const page = document.querySelector('[data-page="overview"]:not([hidden])');
      if (!page?.innerText.includes('Choose notebook cover 00?')) return false;
      try { oracle.compareOverview(state, oracle.renderedOverview(page)); return true; }
      catch { return false; }
    });
    await tick(); // A second animation frame bounds the preceding DOM's first paint.
    const upperBoundMs = performance.now();
    check(upperBoundMs < 2000, 'First real data paint exceeded two seconds');
    return { domFirstDataPaintUpperBoundMs: upperBoundMs,
      paint: performance.getEntriesByType('paint').map(entry => ({ name: entry.name, ms: entry.startTime })),
      overviewKpis: oracle.renderedOverview(document.querySelector('[data-page="overview"]')),
      actualURL: location.origin + location.pathname, snapshotInjected: false };
  }
  check(!state.brain.filling_in && state.spend?.complete === true, 'Metadata and usage must both be complete');
  check(state.spend.read === state.spend.files && state.spend.read > 0, 'All real transcripts must be counted');
  const route = async (view, extra = '') => {
    location.hash = 'view=' + view + extra;
    await wait(() => document.querySelector(`[data-page="${view}"]:not([hidden])`));
    await settle();
    return document.querySelector(`[data-page="${view}"]:not([hidden])`);
  };
  const negativeControls = [], rowEvidence = {};
  const reject = (name, operation, reason) => {
    let error;
    try { operation(); } catch (failure) { error = failure; }
    check(error && reason.test(error.message), 'Negative control was not rejected: ' + name);
    negativeControls.push(name);
  };
  const pages = async (root, expected, rowSelector, projector, name, run) => {
    const frame = root.querySelector('[data-list-frame]');
    check(frame, 'Missing real data list');
    const button = label => [...frame.querySelectorAll('button')].find(b => b.textContent.trim() === label);
    const first = button('First');
    if (first && !first.disabled) { first.click(); await settle(); }
    let count = 0, texts = [], records = [];
    for (let i = 0; i < 200; i++) {
      const rows = [...frame.querySelectorAll(rowSelector)];
      count += rows.length;
      texts.push(...rows.map(row => normalize(row.innerText)));
      if (projector) {
        const actual = rows.map(projector);
        records.push(...actual);
        if (i === 0 && rows.length >= 2) {
          const blank = rows[0].cloneNode(true);
          blank.querySelector('.row-text').textContent = '';
          reject(`${run}/blank-${name}`, () => oracle.compareRows([actual[0]], [projector(blank)]), /Blank display body/);
          reject(`${run}/duplicate-${name}`, () => oracle.compareRows(actual.slice(0, 2), [actual[0], actual[0]]), /Duplicate/);
          const substituted = rows[0].cloneNode(true);
          const context = name === 'step' && substituted.querySelector('.row-line > small')
            ? '.row-line > small' : '.row-line > strong';
          substituted.querySelector(context).textContent = 'Unrelated notebook';
          reject(`${run}/substituted-${name}-project`, () => oracle.compareRows([actual[0]], [projector(substituted)]), /substituted/);
        }
      }
      const next = button('Next');
      check(next, 'Missing pagination control');
      if (next.disabled) break;
      const position = frame.querySelector('.page-position').textContent;
      next.click();
      await wait(() => frame.querySelector('.page-position').textContent !== position);
    }
    check(count === expected.length, `Rendered ${count} rows; backend has ${expected.length}`);
    if (projector) {
      oracle.compareRows(expected, records);
      if (run === 0) rowEvidence[name] = { expected, rendered: records };
      return count;
    }
    const joined = texts.join(' ');
    for (const text of expected) {
      check(normalize(text), 'Blank expected display value');
      check(joined.includes(normalize(text)), 'Backend value absent from paginated DOM: ' + text);
    }
    return count;
  };
  const runs = [];
  for (let run = 0; run < 3; run++) {
    const views = {};
    let root = await route('overview');
    oracle.compareOverview(state, oracle.renderedOverview(root));
    check(root.innerText.includes('Choose notebook cover 00?'), 'Overview lacks real inbox data');
    views.overview = 5;
    views.overviewKpis = oracle.renderedOverview(root);
    const changed = root.cloneNode(true);
    changed.querySelector('.kpi-value').textContent = '999999';
    reject(`${run}/alter-kpi`, () => oracle.compareOverview(state, oracle.renderedOverview(changed)), /Wrong overview KPI/);
    root = await route('projects');
    views.projects = await pages(root, state.nodes.filter(n => !n.archived).map(n => n.title), 'tbody tr');
    root = await route('inbox');
    // Backend needs have shared IDs across due/inbox; the UI intentionally deduplicates them.
    const { inbox, due: steps } = model.needsView(state);
    views.inbox = await pages(root, inbox.map(row => oracle.request(state, row)), '[data-item]', oracle.renderedRequest, 'request', run);
    root = await route('inbox', '&tab=due');
    views.coordinatorSteps = await pages(root, steps.map(row => oracle.step(state, row)), '[data-item]', oracle.renderedStep, 'step', run);
    root = await route('specialists');
    views.specialists = await pages(root, state.specialists.map(row => row.title), '[data-item]');
    root = await route('jobs');
    const jobs = state.nodes.flatMap(n => n.jobs.rows);
    const jobTitle = name => name.replace(/^job\//, '').replace(/[-_/:]+/g, ' ').replace(/^./, c => c.toUpperCase());
    views.jobs = await pages(root, jobs.map(row => jobTitle(row.Job)), 'tbody tr');
    root = await route('threads');
    const threads = state.nodes.flatMap(n => n.work.filter(w => !['merged', 'abandoned', 'done'].includes(w.state)));
    views.threads = await pages(root, threads.map(row => jobTitle(row.branch)), '[data-thread]');
    root = await route('usage');
    for (const row of state.usage) check(root.innerText.includes(row.label), 'Subscription absent from DOM');
    const metric = [...root.querySelectorAll('.metric')].find(el => el.innerText.includes('Processed'));
    const value = state.spend.since_week.processed;
    const formatted = Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(value);
    check(metric?.innerText.includes(formatted), 'Real aggregate total absent from DOM');
    views.usage = { transcripts: state.spend.read, processed: value };
    root = await route('learnings');
    check(state.specialists.every(sp => !sp.lessons.length), 'Fixture now includes specialist lessons; extend oracle');
    views.learnings = await pages(root, state.items.all.map(row => row.title), '[data-item]');
    root = await route('tasks');
    const tasks = state.nodes.flatMap(n => n.tasks.rows);
    check(root.querySelectorAll('[data-task-status]').length === tasks.length, 'Task count differs from backend');
    for (const task of tasks) check(root.innerText.includes(task.title), 'Real task absent from DOM');
    views.tasks = tasks.length;
    root = await route('graph');
    const keys = [...state.nodes.filter(n => !n.archived).map(n => 'node:' + n.id),
      ...state.specialists.map(sp => 'specialist:' + sp.node + '/' + sp.id), ...state.items.all.map(item => 'learning:' + item.id)];
    const rendered = [...root.querySelectorAll('[data-graph-key]')].map(el => el.dataset.graphKey);
    check(JSON.stringify([...rendered].sort()) === JSON.stringify(keys.sort()), 'Graph omits backend entities');
    views.graph = rendered.length;
    views.project = 0;
    views.projectTabs = 0;
    for (const node of state.nodes) {
      root = await route('project', '&node=' + encodeURIComponent(node.id));
      check(document.querySelector('h1')?.innerText === node.title, 'Project route shows the wrong backend node');
      check(!root.innerText.includes('Project unavailable'), 'Real project missing');
      const scoped = '&node=' + encodeURIComponent(node.id);
      root = await route('project', scoped + '&tab=tasks');
      const panel = root.querySelector('[role="tabpanel"]:not([hidden])');
      check(panel, 'Project work tab missing');
      for (const task of node.tasks.rows) check(panel.innerText.includes(task.title), 'Project omits its task');
      views.projectTabs++;
      root = await route('project', scoped + '&tab=jobs');
      const jobsPanel = root.querySelector('[role="tabpanel"]:not([hidden])');
      for (const job of node.jobs.rows) check(jobsPanel.innerText.includes(jobTitle(job.Job)), 'Project omits its job');
      views.projectTabs++;
      root = await route('project', scoped + '&tab=threads');
      const threadsPanel = root.querySelector('[role="tabpanel"]:not([hidden])');
      for (const work of node.work) check(threadsPanel.innerText.includes(jobTitle(work.branch)), 'Project omits its thread');
      views.projectTabs++;
      views.project++;
    }
    runs.push(views);
  }
  await route('overview');
  return { runs, negativeControls, rowEvidence, allViewsComplete: true, actualURL: location.origin + location.pathname,
    browser: navigator.userAgent, injectedSnapshot: false };
}
