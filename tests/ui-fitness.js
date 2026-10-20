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
