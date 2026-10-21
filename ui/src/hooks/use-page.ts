import { useState } from "react";

// Rows and their footer always use the same cursor. Reset it when the list's meaning changes,
// and persist a clamp when data shrinks so a later refresh cannot resurrect an obsolete page.
// Page size is a user preference and survives either kind of cursor correction.
export function usePage(total: number, resetKey = "", initialSize = 10) {
  const [cursor, setCursor] = useState(() => ({ page: 0, size: initialSize, resetKey }));
  const pages = Math.max(1, Math.ceil(total / cursor.size));
  const page = cursor.resetKey === resetKey ? Math.max(0, Math.min(cursor.page, pages - 1)) : 0;
  if (page !== cursor.page || resetKey !== cursor.resetKey) {
    setCursor({ ...cursor, page, resetKey });
  }
  return {
    page, pages, size: cursor.size, start: page * cursor.size, end: (page + 1) * cursor.size,
    setPage: (next: number) => setCursor((previous) => ({ ...previous, page: next, resetKey })),
    setSize: (size: number) => setCursor((previous) => ({ ...previous, page: 0, size, resetKey })),
  };
}
