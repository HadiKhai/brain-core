import { useId, useMemo, useState, type ReactNode } from "react";
import { Combobox as ComboboxPrimitive } from "@base-ui/react";
import { ChevronDown, Search, X } from "lucide-react";
import { Combobox, ComboboxContent, ComboboxEmpty, ComboboxInput, ComboboxItem, ComboboxList } from "@/components/ui/combobox";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { useSearch, fold, type Found } from "./search";
import type { RecordData } from "./model";

// ui-designer §13: the one picker for every field that points at another record.
// Opens with the most relevant records, searches the server as you type (debounced, stale-safe),
// pages on scroll, highlights the match and tells similar names apart with a secondary line.
export function Highlight({ text, query }: { text: string; query: string }) {
  const q = fold(query);
  if (!q) return <>{text}</>;
  // Map folded positions back to the original string, one character at a time.
  const folded: number[] = [];
  let acc = "";
  for (let i = 0; i < text.length; i++) {
    const f = fold(text[i]) || (/\s/.test(text[i]) ? " " : "");
    for (let k = 0; k < f.length; k++) folded.push(i);
    acc += f;
  }
  const at = acc.indexOf(q);
  if (at < 0) return <>{text}</>;
  const start = folded[at], end = (folded[at + q.length - 1] ?? text.length - 1) + 1;
  return <>{text.slice(0, start)}<mark>{text.slice(start, end)}</mark>{text.slice(end)}</>;
}

const nouns: Record<string, string> = { node: "projects", specialist: "specialists", learning: "learnings", job: "jobs", all: "records" };

export function RecordPicker({ s, kind, label, value, onChange, placeholder, allLabel, hideLabel = false }: {
  s: RecordData; kind: string; label: string; value: Found | null; onChange: (v: Found | null) => void;
  placeholder?: string; allLabel?: string; hideLabel?: boolean;
}) {
  const id = useId();
  const [open, setOpen] = useState(false), [query, setQuery] = useState("");
  const found = useSearch(s, kind, query, open);
  const items = useMemo(() => found.items, [found.items]);
  return (
    <div className="field picker-field">
      <label htmlFor={id} className={hideLabel ? "sr-only" : ""}>{label}</label>
      <Combobox
        items={items}
        filter={null}
        value={value}
        onValueChange={(v) => { onChange((v as Found) || null); setQuery(""); }}
        open={open}
        onOpenChange={(o) => { setOpen(o); if (!o) setQuery(""); }}
        inputValue={query}
        onInputValueChange={(v) => setQuery(v)}
        itemToStringLabel={(v: Found) => v?.title || ""}
        isItemEqualToValue={(a: Found, b: Found) => a?.kind === b?.kind && a?.id === b?.id}
      >
        <div className="picker-control">
          <ComboboxPrimitive.Trigger id={id} aria-label={label} className="picker-trigger">
            {value ? (
              <span className="picker-chip">
                <span className="picker-chip-title" dir="auto">{value.title}</span>
                <small>{value.secondary}</small>
              </span>
            ) : <span className="picker-placeholder">{allLabel || placeholder || `Choose ${nouns[kind] || "a record"}`}</span>}
            <ChevronDown size={14} />
          </ComboboxPrimitive.Trigger>
          {value && <Button variant="ghost" size="icon-xs" className="picker-clear" aria-label={`Clear ${label}`} onClick={() => onChange(null)}><X size={14} /></Button>}
        </div>
        <ComboboxContent className="picker-popup">
          <ComboboxInput showTrigger={false} placeholder={placeholder || `Search ${nouns[kind] || "records"}`} aria-label={`Search ${label}`} className="picker-search">
          </ComboboxInput>
          <PickerStatus state={found.state} total={found.total} query={query} onRetry={found.retry} onClear={() => setQuery("")} />
          <ComboboxList
            className="picker-list"
            onScroll={(e) => {
              const el = e.currentTarget;
              if (el.scrollTop + el.clientHeight > el.scrollHeight - 48) found.more();
            }}
          >
            {(item: Found) => (
              <ComboboxItem key={item.kind + item.id} value={item} className="picker-item">
                <span className="picker-item-text">
                  <span className="picker-item-title" dir="auto"><Highlight text={item.title} query={query} /></span>
                  <small dir="auto">{item.secondary}</small>
                </span>
              </ComboboxItem>
            )}
          </ComboboxList>
          {found.state === "more" && <div className="picker-more" role="status">Loading more…</div>}
          <ComboboxEmpty>{found.state === "idle" && query ? null : null}</ComboboxEmpty>
        </ComboboxContent>
      </Combobox>
    </div>
  );
}

function PickerStatus({ state, total, query, onRetry, onClear }: { state: string; total: number; query: string; onRetry: () => void; onClear: () => void }): ReactNode {
  if (state === "loading") return <div className="picker-loading" aria-label="Loading results">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-9 rounded-md" />)}</div>;
  if (state === "error") return <div className="picker-message" role="alert">Search failed. <Button variant="link" size="sm" onClick={onRetry}>Retry</Button></div>;
  if (!total && query) return <div className="picker-message" role="status">No match for “{query}”. <Button variant="link" size="sm" onClick={onClear}>Clear search</Button></div>;
  if (!total) return <div className="picker-message" role="status">Nothing to choose yet.</div>;
  return null;
}

export { Search as PickerIcon };
