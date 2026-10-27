import { useState } from "react";

// A page can expand by ten rows without moving its start or changing the page-size preference.
// Navigation, filtering and page-size changes start a new window; live shrink clamps the cursor.
export function usePage(total: number, resetKey = "", initialSize = 10) {
  const [cursor, setCursor] = useState(() => ({ page: 0, size: initialSize, extra: 0, resetKey, total }));
  const pages = Math.max(1, Math.ceil(total / cursor.size));
  const page = cursor.resetKey === resetKey ? Math.max(0, Math.min(cursor.page, pages - 1)) : 0;
  const corrected = page !== cursor.page || resetKey !== cursor.resetKey || total < cursor.total;
  if (corrected || total !== cursor.total) setCursor({ ...cursor, page, extra: corrected ? 0 : cursor.extra, resetKey, total });
  const start = page * cursor.size;
  const end = Math.min(total, start + cursor.size + (corrected ? 0 : cursor.extra));
  return {
    page, pages, size: cursor.size, start, end,
    setPage: (next: number) => setCursor(previous => ({ ...previous, page: next, extra: 0, resetKey })),
    setSize: (size: number) => setCursor(previous => ({ ...previous, page: 0, size, extra: 0, resetKey })),
    showMore: () => setCursor(previous => ({ ...previous, extra: previous.extra + 10, resetKey })),
  };
}
