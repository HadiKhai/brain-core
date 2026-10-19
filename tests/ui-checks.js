// Evaluate in a throwaway `brain ui` browser page. No packages, native notifications, or file writes.
(() => {
  const saved = { state: S, selected, url: location.href, notifyOn, firstPaint, attention: previousAttention, notification: window.Notification };
  const checks = [];
  function check(condition, name) {
    if (!condition) throw new Error(name);
    checks.push(name);
  }
  function expected(date, zone, withDate = false) {
    return new Intl.DateTimeFormat(undefined, {
      hour: "2-digit", minute: "2-digit", timeZone: zone,
      ...(withDate ? { year: "numeric", month: "short", day: "numeric" } : {}),
    }).format(date);
  }
  try {
    S = structuredClone(S);
    notifyOn = false;
    for (const zone of ["UTC", "Asia/Beirut", "America/New_York"]) {
      S.brain.tz = zone;
      for (const stamp of ["2026-10-19T06:00:00Z", "2026-01-19T06:00:00Z"]) {
        check(brainTime(stamp) === expected(new Date(stamp), zone), `clock ${zone} ${stamp}`);
      }
      renderHeat();
      const tick = document.querySelector("#heat .tick");
      check(tick && tick.textContent === expected(new Date(S.activity.start * 1000), zone), `chart ${zone}`);
      const node = S.nodes[0], instant = new Date("2026-10-19T06:00:00Z");
      node.decisions = [{ title: "Timezone decision", time: "wrong raw timezone", t: instant.getTime() / 1000 }];
      node.jobs.rows = [{ Job: "timezone-job", Started: "2026-10-19T09:00:00+03:00", Hours: "1", Outcome: "merged", Reviews: "1" }];
      openDetail(node.id);
      const detail = document.getElementById("detail");
      check(Array.from(detail.querySelectorAll(".when")).some(e => e.textContent === expected(instant, zone, true)), `decision ${zone}`);
      const row = Array.from(detail.querySelectorAll("tr")).find(e => e.textContent.includes("timezone-job"));
      check(row && row.children[1].textContent === expected(instant, zone, true), `job ${zone}`);
    }
    closeDetail();
    check(location.search === new URL(saved.url).search, "closing notes preserves the API key");
    const events = [];
    window.Notification = class { constructor(title, options) { events.push({ title, options }); } };
    notifyOn = true;
    firstPaint = true;
    const question = { node: "test", kind: "inbox", text: "Choose a date" };
    S.attention = [question];
    notifyNew();
    check(events.length === 0, "initial attention is quiet");
    firstPaint = false;
    S.attention = [];
    notifyNew();
    S.attention = [question];
    notifyNew();
    notifyNew();
    check(events.length === 1, "new attention notifies once");
    S.attention = [];
    notifyNew();
    S.attention = [question];
    notifyNew();
    check(events.length === 2, "attention returning after resolution notifies again");
    return { passed: checks.length, checks };
  } finally {
    S = saved.state;
    selected = saved.selected;
    firstPaint = saved.firstPaint;
    previousAttention = saved.attention;
    window.Notification = saved.notification;
    notifyOn = false;
    history.replaceState(null, "", saved.url);
    render();
    if (!selected) closeDetail();
    notifyOn = saved.notifyOn;
    notifyLabel();
  }
})()
