import type { ReactNode } from "react";
import { ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Tip } from "./shared";

// Data before words (ui-designer §14): numbers with trends, one mark per item, compact sparklines.
export function Kpi({ label, value, note, trend, children, big = false }: {
  label: string; value: ReactNode; note?: ReactNode; trend?: { delta: number; text: string; good?: "up" | "down" }; children?: ReactNode; big?: boolean;
}) {
  const good = trend ? (trend.good === "down" ? trend.delta < 0 : trend.delta > 0) : false;
  return (
    <div className={"kpi" + (big ? " kpi-big" : "")} data-kpi>
      <span className="kpi-label">{label}</span>
      <strong className="kpi-value">{value}</strong>
      {(trend || note) && (
        <span className="kpi-foot">
          {trend && (
            <span className={"trend " + (trend.delta === 0 ? "flat" : good ? "good" : "bad")} data-trend>
              {trend.delta > 0 ? <ArrowUpRight size={12} /> : trend.delta < 0 ? <ArrowDownRight size={12} /> : <Minus size={12} />}
              {trend.text}
            </span>
          )}
          {note && <span className="kpi-note">{note}</span>}
        </span>
      )}
      {children}
    </div>
  );
}

export type Mark = { key: string; tone: string; label: string };
export const tones: Record<string, string> = { ready: "Ready to merge", review: "In review", working: "Working", blocked: "Needs you", idle: "Idle working copy" };
// One mark per item, coloured by state, with a legend that counts each state.
export function Segmented({ marks, caption, order = Object.keys(tones), names = tones }: { marks: Mark[]; caption: string; order?: string[]; names?: Record<string, string> }) {
  const counts = order.map((t) => [t, marks.filter((m) => m.tone === t).length] as const).filter(([, n]) => n);
  return (
    <div className="segmented" data-segmented>
      <div className="marks" role="img" aria-label={caption + ": " + counts.map(([t, n]) => `${n} ${names[t]}`).join(", ")}>
        {[...marks].sort((a, b) => order.indexOf(a.tone) - order.indexOf(b.tone)).map((m) => (
          <Tip key={m.key} text={m.label}><i className={"mark tone-" + m.tone} /></Tip>
        ))}
        {!marks.length && <span className="marks-empty">Nothing open</span>}
      </div>
      <div className="legend">
        {counts.map(([t, n]) => <span key={t}><i className={"swatch tone-" + t} />{names[t]} <b>{n}</b></span>)}
        <span className="legend-caption">{caption}</span>
      </div>
    </div>
  );
}

export function Spark({ values, label }: { values: number[]; label: string }) {
  const max = Math.max(1, ...values);
  return (
    <span className="spark" role="img" aria-label={label}>
      {values.map((v, i) => <i key={i} style={{ height: `${Math.max(8, (v / max) * 100)}%` }} className={v ? "on" : ""} />)}
    </span>
  );
}

// A section whose views are tabs: one surface, one list at a time.
export function SectionTabs({ tabs, value, onValue, action }: {
  tabs: { id: string; label: string; count?: number; body: ReactNode }[]; value?: string; onValue?: (v: string) => void; action?: ReactNode;
}) {
  return (
    <Tabs value={value} defaultValue={value === undefined ? tabs[0]?.id : undefined} onValueChange={(v) => onValue?.(String(v))} className="section-tabs">
      <div className="tabs-bar">
        <TabsList>
          {tabs.map((t) => (
            <TabsTrigger key={t.id} value={t.id}>
              {t.label}
              {t.count != null && <span className="tab-count">{t.count}</span>}
            </TabsTrigger>
          ))}
        </TabsList>
        {action}
      </div>
      {tabs.map((t) => <TabsContent key={t.id} value={t.id}>{t.body}</TabsContent>)}
    </Tabs>
  );
}

export function ListFooter({ shown, total, noun, action }: { shown: number; total: number; noun: string; action?: ReactNode }) {
  return (
    <footer className="list-footer">
      <span>Showing {shown} of {total} {noun}</span>
      {action}
    </footer>
  );
}
