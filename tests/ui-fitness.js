// Evaluate after each view/theme/viewport change. Measurements are from rendered boxes.
(() => {
  const visible = (e) => {
    if (e.closest("[aria-hidden=true]")) return false;
    const r = e.getBoundingClientRect();
    return (
      r.width > 0 &&
      r.height > 0 &&
      r.bottom > 0 &&
      r.top < innerHeight &&
      getComputedStyle(e).visibility !== "hidden" &&
      !e.closest("[hidden]")
    );
  };
  // Compare rendered anchors with the design inset, not their current padding: a
  // missing-padding mutation must fail rather than redefine its own expectation.
  const alignment = { anchors: 0, headers: 0, failures: [] };
  const rendered = e => !!e.getClientRects().length && !e.closest('[hidden],[aria-hidden=true]');
  const near = (actual, expected, label) => {
    alignment.anchors++;
    if (Math.abs(actual - expected) > 1) alignment.failures.push({label, actual, expected});
  };
  const textStart = (e, rtl = false) => {
    const walker = document.createTreeWalker(e, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) if (node.textContent.trim()) {
      const range = document.createRange(); range.selectNodeContents(node);
      const rect = range.getBoundingClientRect();
      return rtl ? rect.right : rect.left;
    }
    return e.getBoundingClientRect().left;
  };
  for (const row of document.querySelectorAll('.section-body .section-content')) {
    if (!rendered(row)) continue;
    const expected = row.getBoundingClientRect().left + 20;
    for (const block of row.querySelectorAll(':scope > .text-preview,:scope > .prose-preview,:scope > strong,:scope > small,:scope > p'))
      near(block.getBoundingClientRect().left, expected, 'section content inset');
    for (const action of row.querySelectorAll(':scope > .reader-trigger,:scope > .action-link-group > .item-action,:scope > .text-preview > .reader-trigger'))
      near(textStart(action), expected, 'section action text inset');
    for (const preview of row.querySelectorAll(':scope > .text-preview > .prose-preview'))
      near(preview.getBoundingClientRect().left, expected, 'section preview inset');
  }
  // All preview text and text-only reader actions share their local column's
  // start; structured tables and inline action groups retain their own columns.
  for (const preview of document.querySelectorAll('.section-body .text-preview')) {
    if (!rendered(preview)) continue;
    const frame = preview.getBoundingClientRect();
    for (const block of preview.querySelectorAll('.preview-markdown .markdown :is(p,li,h1,h2,h3)')) {
      let indent = 0;
      for (let parent = block.parentElement; parent && !parent.classList.contains('markdown'); parent = parent.parentElement)
        if (parent.matches('ul,ol')) indent += 20;
      const rtl = getComputedStyle(block).direction === 'rtl';
      near(textStart(block, rtl), rtl ? frame.right - indent : frame.left + indent, 'section rendered Markdown text inset');
    }
  }
  for (const action of document.querySelectorAll('.section-body .reader-trigger'))
    if (rendered(action)) near(textStart(action), action.getBoundingClientRect().left, 'reader action has no extra text indent');
  for (const row of document.querySelectorAll('.section-body .correspondence')) {
    if (!rendered(row)) continue;
    const frame = row.getBoundingClientRect(), body = row.closest('.section-body').getBoundingClientRect();
    near(frame.left, body.left + (innerWidth < 768 ? 16 : 20), 'correspondence section inset');
    const start = frame.left + (innerWidth < 768 ? 14 : 18);
    for (const block of row.querySelectorAll(':scope > .correspondence-heading,:scope > .correspondence-excerpt,:scope > .recommendation,:scope > .correspondence-actions'))
      near(block.getBoundingClientRect().left, start, 'correspondence content and action column');
  }
  for (const body of document.querySelectorAll('.section-body')) {
    if (!rendered(body)) continue;
    for (const count of body.querySelectorAll(':scope > .table-footer > span'))
      near(count.getBoundingClientRect().left, body.getBoundingClientRect().left + 20, 'section footer count inset');
    for (const bare of body.querySelectorAll(':scope > .text-preview,:scope > p,:scope > .readable'))
      alignment.failures.push({label:'section content has no padded row', text:bare.textContent.slice(0,40)});
    for (const header of body.querySelectorAll(':scope > .disclosure > .disclosure-button')) {
      alignment.headers++;
      const b = body.getBoundingClientRect(), h = header.getBoundingClientRect();
      near(h.left, b.left + body.clientLeft, 'group header start');
      near(h.width, body.clientWidth, 'group header spans container');
    }
  }
  const activePage = document.querySelector('[data-page]:not([hidden])');
  if (activePage) {
    const p = activePage.getBoundingClientRect(), h = document.querySelector('.page-header').getBoundingClientRect();
    near(p.left, h.left, 'shared page start'); near(p.right, h.right, 'shared page end');
  }
  const cards = [...document.querySelectorAll('.attention-split > .page-section')].filter(rendered);
  if (cards.length === 2 && Math.abs(cards[0].getBoundingClientRect().top - cards[1].getBoundingClientRect().top) <= 1)
    near(cards[0].getBoundingClientRect().bottom, cards[1].getBoundingClientRect().bottom, 'attention row card bottoms');
  if (alignment.failures.length) throw Error('Section alignment: ' + JSON.stringify(alignment));
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 1;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  const rgba = (color) => {
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = color;
    ctx.fillRect(0, 0, 1, 1);
    return [...ctx.getImageData(0, 0, 1, 1).data].map((v, i) =>
      i === 3 ? v / 255 : v / 255,
    );
  };
  const mix = (fg, bg) => [
    ...fg.slice(0, 3).map((v, i) => v * fg[3] + bg[i] * (1 - fg[3])),
    1,
  ];
  const bgFor = (e) => {
    const chain = [];
    for (let n = e; n; n = n.parentElement) chain.push(n);
    let bg = [1, 1, 1, 1];
    for (const n of chain.reverse())
      bg = mix(rgba(getComputedStyle(n).backgroundColor), bg);
    return bg;
  };
  const lum = (rgb) =>
    rgb
      .slice(0, 3)
      .map((v) =>
        v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4),
      )
      .reduce((a, v, i) => a + v * [0.2126, 0.7152, 0.0722][i], 0);
  const contrastFailures = [],
    measured = [];
  for (const e of document.querySelectorAll("body *")) {
    if (
      !visible(e) ||
      e.closest(
        "svg,script,style,[aria-hidden=true],[disabled],[aria-disabled=true]",
      ) ||
      !Array.from(e.childNodes).some(
        (n) => n.nodeType === Node.TEXT_NODE && n.textContent.trim(),
      )
    )
      continue;
    const cs = getComputedStyle(e),
      bg = bgFor(e),
      fg = mix(rgba(cs.color), bg),
      l1 = lum(fg),
      l2 = lum(bg),
      ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05),
      large =
        parseFloat(cs.fontSize) >= 24 ||
        (parseFloat(cs.fontSize) >= 18.66 && Number(cs.fontWeight) >= 700),
      minimum = large ? 3 : 4.5;
    const record = {
      text: e.textContent.trim().slice(0, 70),
      ratio: Math.round(ratio * 100) / 100,
      minimum,
    };
    measured.push(record);
    if (ratio + 0.02 < minimum)
      contrastFailures.push({
        ...record,
        color: cs.color,
        background: cs.backgroundColor,
      });
  }
  const scroller = document.querySelector("#workspace-content"),
    topbar = document.querySelector(".topbar"),
    dock = document.querySelector("#action-dock-host"),
    page = document.querySelector(".page-header");
  const rect = (e) => {
    const r = e.getBoundingClientRect();
    return {
      x: r.x,
      y: r.y,
      width: r.width,
      height: r.height,
      bottom: r.bottom,
    };
  };
  const fields = [
    ...document.querySelectorAll("input:not([readonly]),textarea"),
  ].filter(visible);
  const missingFieldLabels = fields.filter(
    (e) => !e.getAttribute("aria-label") && !e.labels?.length,
  ).length;
  const missingPlaceholders = fields.filter(
    (e) => !["checkbox", "radio", "hidden"].includes(e.type) && !e.placeholder,
  ).length;
  const controls = [
    ...document.querySelectorAll(
      "button,input,[role=checkbox],[role=combobox],a",
    ),
  ]
    .filter(visible)
    .map((e) => ({
      type: e.getAttribute("role") || e.tagName.toLowerCase(),
      label:
        e.getAttribute("aria-label") ||
        e.getAttribute("aria-labelledby") ||
        e.textContent.trim() ||
        e.labels?.[0]?.textContent ||
        "",
      slot: e.getAttribute("data-slot"),
    }));
  const clippedStatuses = [...document.querySelectorAll(".status")]
    .filter((e) => visible(e) && e.scrollHeight > e.clientHeight + 1)
    .map((e) => e.textContent);
  if (clippedStatuses.length)
    throw Error("Clipped status text: " + clippedStatuses.join("; "));
  const result = {
    viewport: [innerWidth, innerHeight],
    theme: document.documentElement.classList.contains("dark")
      ? "dark"
      : "light",
    view: document.querySelector("h1")?.textContent,
    contrast: {
      measured: measured.length,
      minimum: Math.min(...measured.map((r) => r.ratio)),
      failures: contrastFailures,
    },
    geometry: {
      scroller: rect(scroller),
      topbar: rect(topbar),
      page: rect(page),
      dock: dock && visible(dock) ? rect(dock) : null,
      gutter: parseFloat(getComputedStyle(scroller).paddingLeft),
      documentOverflow: document.documentElement.scrollWidth > innerWidth,
      insetOverflow: scroller.scrollWidth > scroller.clientWidth + 1,
    },
    fields: {
      count: fields.length,
      missingFieldLabels,
      missingPlaceholders,
      smallFont:
        innerWidth < 768
          ? fields.filter(
              (e) =>
                !["checkbox", "radio", "hidden"].includes(e.type) &&
                parseFloat(getComputedStyle(e).fontSize) < 16,
            ).length
          : 0,
    },
    controls,
    alignment,
  };
  if (
    contrastFailures.length ||
    missingFieldLabels ||
    missingPlaceholders ||
    result.fields.smallFont ||
    result.geometry.documentOverflow ||
    result.geometry.insetOverflow
  )
    throw Error(JSON.stringify(result));
  return result;
})();
