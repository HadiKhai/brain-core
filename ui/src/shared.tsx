import { usePage } from "./hooks/use-page";
import {
  createContext,
  Children,
  useEffect,
  useContext,
  useState,
  useId,
  isValidElement,
  type ReactElement,
  type ReactNode,
  type CSSProperties,
} from "react";
import ReactMarkdown from "react-markdown";
import { createPortal } from "react-dom";
import {
  ChevronDown,
  ChevronRight,
  Copy,
  Inbox,
  Search,
  ArrowUpDown,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
} from "@/components/ui/input-group";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from "@/components/ui/sheet";
import { label, readable, humanText, safeHref, type RecordData } from "./model";
export const TimeZoneContext = createContext<string | undefined>(undefined);
export function ScreenAction({ active, children }: { active: boolean; children: ReactNode }) {
  const [host, setHost] = useState<HTMLElement | null>(null);
  useEffect(() => setHost(document.getElementById("screen-action-host")), []);
  return active && host ? createPortal(children, host) : null;
}
// Opens a conversation in a new tab and says what happens next; the link stays to open it again.
export function ActionLink({ href, children, primary = false }: { href: string; children: ReactNode; primary?: boolean }) {
  const [opened, setOpened] = useState(false);
  return <span className="action-link-group"><a className={primary ? "primary-link" : "item-action"} href={safeHref(href)} target="_blank" rel="noreferrer" onClick={() => setOpened(true)}>{opened ? "Open again" : children}</a>{opened && <small role="status">Continue in the conversation. Return here when you’re done.</small>}</span>;
}

export function ActionDock({ children }: { children: ReactNode }) {
  return <div className="action-dock">{children}</div>;
}

export type Crumb = { label: string; onClick?: () => void };
// One title per page. A breadcrumb appears only below the top level and never repeats the title.
export function PageHeader({
  title,
  subtitle,
  crumbs = [],
  actions,
}: {
  title: string;
  subtitle?: ReactNode;
  crumbs?: Crumb[];
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div className="page-heading">
        {crumbs.length > 0 && (
          <nav aria-label="Breadcrumb" className="crumbs">
            {crumbs.map((c, i) => (
              <span key={i}>
                {c.onClick ? <Button variant="link" size="xs" className="crumb" onClick={c.onClick}>{c.label}</Button> : c.label}
                <ChevronRight size={12} />
              </span>
            ))}
          </nav>
        )}
        <h1 dir="auto">{title}</h1>
        {subtitle && <p className="page-meta">{subtitle}</p>}
      </div>
      {actions && <div className="actions">{actions}</div>}
    </header>
  );
}
export function PageSection({
  title,
  description,
  action,
  children,
  className = "",
}: {
  title: string;
  description?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={"page-section " + className}>
      <div className="section-heading">
        <div>
          <h2>{title}</h2>
          {description && <p>{description}</p>}
        </div>
        {action}
      </div>
      <div className="section-body">{children}</div>
    </section>
  );
}
export function Empty({
  text,
  action = "New activity will appear here.",
  icon: Icon = Inbox,
}: {
  text: string;
  action?: string;
  icon?: any;
}) {
  return (
    <div className="empty">
      <span className="empty-icon">
        <Icon size={22} />
      </span>
      <strong>{text}</strong>
      <p>{action}</p>
    </div>
  );
}
export function PagedCollection({ children, name }: { children: ReactNode; name: string }) {
  const items = Children.toArray(children);
  const { page: current, pages, setPage, start, end } = usePage(items.length, name);
  return <>{items.slice(start, end)}{items.length > 10 && <footer className="table-footer"><span>{current * 10 + 1}–{Math.min(items.length, current * 10 + 10)} of {items.length} {name}</span><div className="pager"><Button variant="outline" size="sm" disabled={!current} onClick={() => setPage(current - 1)}>Previous</Button><span>{current + 1} / {pages}</span><Button variant="outline" size="sm" disabled={current === pages - 1} onClick={() => setPage(current + 1)}>Next</Button></div></footer>}</>;
}
export function Status({ children }: { children: ReactNode }) {
  const zone = useContext(TimeZoneContext);
  const raw = String(children || "Not recorded");
  const t = /^[-—\s]*$/.test(raw) ? "State not recorded" : raw;
  return (
    <Badge
      variant="secondary"
      className={
        "status " +
        (/near|review|stale|paused|unknown|proposed|unverified/i.test(t)
          ? "warning"
          : /blocked|stop|error|abandoned|missing|critical/i.test(t)
            ? "danger"
            : /ready|merged|active|normal|accepted|live|complete/i.test(t)
              ? "good"
              : "")
      }
    >
      <span className="status-dot" />
      {humanText(readable(label(t), zone))}
    </Badge>
  );
}
export function Tip({ text, children, style }: { text: string; children: ReactNode; style?: CSSProperties }) {
  return (
    <Tooltip>
      {isValidElement(children) &&
      (children.type === Button || children.type === "button") ? (
        <TooltipTrigger render={children as ReactElement} />
      ) : (
        <TooltipTrigger render={<span tabIndex={0} className="tip-trigger" style={style} />}>
          {children}
        </TooltipTrigger>
      )}

      <TooltipContent className="max-w-sm break-words">{humanText(text)}</TooltipContent>
    </Tooltip>
  );
}
export function Text({ children, zone }: { children: any; zone?: string }) {
  const workspaceZone = useContext(TimeZoneContext);
  const text = humanText(readable(children, zone || workspaceZone));
  const rtl = /[\u0590-\u08ff]/.test(text.match(/[A-Za-z\u0590-\u08ff]/)?.[0] || "");
  // `inline code` reads as code, never as raw backticks.
  const parts = text.split(/`([^`\n]+)`/);
  return (
    <span className={"readable" + (rtl ? " rtl-text" : "")} dir="auto">
      {parts.map((part, i) => (i % 2 ? <code key={i} className="inline-code" dir="ltr">{part}</code> : part))}
    </span>
  );
}
export function Markdown({ text }: { text: any }) {
  const zone = useContext(TimeZoneContext);
  return (
    <div className="markdown" dir="auto">
      <ReactMarkdown
        components={{
          p: ({ children }) => <p dir="auto">{children}</p>,
          li: ({ children }) => <li dir="auto">{children}</li>,
          h1: ({ children }) => <h1 dir="auto">{children}</h1>,
          h2: ({ children }) => <h2 dir="auto">{children}</h2>,
          h3: ({ children }) => <h3 dir="auto">{children}</h3>,
          a: ({ href, children }) =>
            safeHref(href) ? (
              <a href={safeHref(href)} rel="noreferrer">
                {children}
              </a>
            ) : (
              <span>{children}</span>
            ),
          img: ({ alt }) => <span>{alt}</span>,
        }}
      >
        {humanText(readable(text, zone))}
      </ReactMarkdown>
    </div>
  );
}
export function Disclosure({
  title,
  children,
  initial = false,
}: {
  title: string;
  children: ReactNode;
  initial?: boolean;
}) {
  return (
    <Collapsible defaultOpen={initial} className="disclosure">
      <CollapsibleTrigger
        render={<Button variant="ghost" className="disclosure-button" />}
      >
        <ChevronDown size={14} />
        {title}
      </CollapsibleTrigger>
      <CollapsibleContent>{children}</CollapsibleContent>
    </Collapsible>
  );
}
export function Command({ text, label: caption = "Copy", disabled = false }: { text: string; label?: string; disabled?: boolean }) {
  const [copied, setCopied] = useState(caption);
  useEffect(() => setCopied(caption), [text, caption]);
  return (
    <div className="command">
      <Input
        readOnly
        value={text}
        aria-label="Command to copy"
        onClick={(e) => e.currentTarget.select()}
      />
      <Button
        variant="outline"
        disabled={disabled}
        onClick={async () => {
          try {
            setCopied("Copying…");
            await navigator.clipboard.writeText(text);
            setCopied("Copied");
          } catch {
            setCopied("Copy failed · select the text");
          }
        }}
      >
        <Copy size={14} />
        <span role="status">{copied}</span>
      </Button>
    </div>
  );
}
export function Choice({
  name,
  value,
  onChange,
  options,
  disabled = false,
}: {
  name: string;
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>{name}</label>
      <Select
        disabled={disabled}
        items={options}
        value={value}
        onValueChange={(v) => onChange(String(v))}
      >
        <SelectTrigger id={id} aria-label={name}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map((o) => (
            <Tooltip key={o.value}>
              <TooltipTrigger render={<SelectItem value={o.value} />}>
                <span className="option-label" dir="auto">{humanText(o.label)}</span>
              </TooltipTrigger>
              <TooltipContent>{humanText(o.label)}</TooltipContent>
            </Tooltip>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
export function FilterToolbar({
  query,
  onQuery,
  placeholder,
  children,
}: {
  query: string;
  onQuery: (v: string) => void;
  placeholder: string;
  children?: ReactNode;
}) {
  return (
    <div className="filter-toolbar">
      <InputGroup>
        <InputGroupAddon>
          <Search size={16} />
        </InputGroupAddon>
        <InputGroupInput
          aria-label={placeholder}
          placeholder={placeholder}
          value={query}
          onChange={(e) => onQuery(e.target.value)}
        />
      </InputGroup>
      {children}
    </div>
  );
}
export type Column = {
  key: string;
  label: string;
  render?: (row: RecordData) => ReactNode;
  sortValue?: (row: RecordData) => string | number;
  numeric?: boolean;
};
export function Reader({ title, text, trigger = "Read full", children, footer, open: controlled, onOpenChange, primary = false }: {
  title: string; text: string; trigger?: string; children?: ReactNode; footer?: ReactNode; open?: boolean; onOpenChange?: (open: boolean) => void; primary?: boolean;
}) {
  const [internal, setInternal] = useState(false);
  const open = controlled ?? internal;
  const setOpen = (value: boolean) => { setInternal(value); onOpenChange?.(value); };
  const zone = useContext(TimeZoneContext);
  return <>
    <Button variant={primary ? "default" : "ghost"} size="sm" className={primary ? "" : "reader-trigger"} onClick={() => setOpen(true)}>{trigger}</Button>
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetContent className="reading-sheet" initialFocus={() => document.querySelector<HTMLElement>(".reading-sheet [data-slot=sheet-close]")}>
        <SheetHeader><SheetTitle><Tip text={title}><Text>{title}</Text></Tip></SheetTitle><SheetDescription>Read the full context, then return to your place.</SheetDescription></SheetHeader>
        <div className="reader-body"><Markdown text={text} />{children}
          {humanText(readable(text, zone)) !== readable(text, zone) && <Disclosure title="Technical details"><pre className="verbatim" dir="ltr">{text}</pre></Disclosure>}
        </div>
        {footer && <div className="reader-footer">{footer}</div>}
      </SheetContent>
    </Sheet>
  </>;
}
export function Preview({ text, title = "Note" }: { text: any; title?: string }) {
  return <div className="text-preview"><div className="prose-preview preview-markdown" dir="auto"><Markdown text={text} /></div><Reader title={title} text={String(text || "")} trigger="Read note" /></div>;
}
export function SectionNote({ text, title = "Note" }: { text: any; title?: string }) {
  return <SectionBodyRow className="note-row"><Preview text={text} title={title} /></SectionBodyRow>;
}
export function SectionBodyRow({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`section-content ${className}`}>{children}</div>;
}
export function TechnicalDetails({ values }: { values: Record<string, any> }) {
  return <Disclosure title="Technical details"><dl className="technical-facts">{Object.entries(values).filter(([,v]) => v != null && v !== "").map(([key, value]) => <div key={key}><dt>{key}</dt><dd className="verbatim" dir="ltr">{String(value)}</dd></div>)}</dl></Disclosure>;
}
function nodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join(" ");
  if (isValidElement<{ children?: ReactNode }>(node)) return nodeText(node.props.children);
  return "";
}
export function DataTable({
  columns,
  rows,
  name = "records",
  details = true,
  resetKey = "",
}: {
  columns: Column[];
  rows: RecordData[];
  name?: string;
  details?: boolean;
  resetKey?: string;
}) {
  const [sort, setSort] = useState({ key: "", direction: 1 });
  const { page: actual, pages, size, setPage, setSize, start, end } = usePage(rows.length, JSON.stringify([name, resetKey, sort.key, sort.direction]));
  const sortColumn = columns.find((c) => c.key === sort.key);
  const value = (r: RecordData) =>
    sortColumn?.sortValue ? sortColumn.sortValue(r) : (r[sort.key] ?? "");
  const sorted = [...rows].sort((a, b) =>
    sort.key
      ? String(value(a)).localeCompare(String(value(b)), undefined, {
          numeric: true,
        }) * sort.direction
      : 0,
  );
  const shown = sorted.slice(start, end);
  return !rows.length ? (
    <Empty text={"No " + name + " yet."} />
  ) : (
    <div className="data-table">
      <div className="mobile-table-sort">
        <Choice
          name="Sort by"
          value={sort.key || "default"}
          onChange={(key) =>
            setSort({ key: key === "default" ? "" : key, direction: 1 })
          }
          options={[
            { value: "default", label: "Original order" },
            ...columns.map((c) => ({ value: c.key, label: c.label })),
          ]}
        />
        <Button
          variant="outline"
          size="sm"
          aria-label="Reverse sort order"
          disabled={!sort.key}
          onClick={() =>
            setSort({
              ...sort,
              key: sort.key || columns[0].key,
              direction: -sort.direction,
            })
          }
        >
          <ArrowUpDown size={14} />
        </Button>
      </div>
      <Table>
        <TableHeader>
          <TableRow>
            {columns.map((c) => (
              <TableHead
                key={c.key}
                aria-sort={
                  sort.key === c.key
                    ? sort.direction === 1
                      ? "ascending"
                      : "descending"
                    : "none"
                }
                className={c.numeric ? "numeric" : ""}
              >
                <Button
                  variant="ghost"
                  onClick={() =>
                    setSort({
                      key: c.key,
                      direction: sort.key === c.key ? -sort.direction : 1,
                    })
                  }
                >
                  {c.label}
                  <ArrowUpDown size={12} />
                </Button>
              </TableHead>
            ))}
            {details && <TableHead><span className="sr-only">Details</span></TableHead>}
          </TableRow>
        </TableHeader>
        <TableBody>
          {shown.map((r, i) => (
            <TableRow key={r._key || r.id || i}>
              {columns.map((c) => {
                const content = c.render ? c.render(r) : <Text>{r[c.key] ?? "Not recorded"}</Text>;
                const text = nodeText(content);
                return (
                <TableCell
                  key={c.key}
                  data-label={c.label}
                  className={c.numeric ? "numeric" : ""}
                >
                  <div className="cell-preview" dir="auto">{content}</div>
                  {text.length > 160 && <Reader title={c.label} text={text} />}
                </TableCell>
              );})}
              {details && <TableCell data-label="Details"><Reader title={name === "hires" ? "Hire brief" : "Job details"} trigger={name === "hires" ? "Read brief" : "Check job"} text={columns.map(c => `## ${c.label}\n${nodeText(c.render ? c.render(r) : String(r[c.key] ?? "Not recorded"))}`).join("\n\n")}><TechnicalDetails values={{Job:r.Job || r.job, Started:r.Started || r.started}} /></Reader></TableCell>}
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <footer className="table-footer">
        <span>
          {actual * Number(size) + 1}–
          {Math.min((actual + 1) * Number(size), rows.length)} of {rows.length}{" "}
          {name}
        </span>
        <div className="pager">
          <Choice
            name="Rows"
            value={String(size)}
            onChange={(v) => {
              setSize(Number(v));
              setPage(0);
            }}
            options={["10", "25", "50"].map((v) => ({ value: v, label: v }))}
          />
          <Button
            variant="outline"
            size="sm"
            disabled={!actual}
            onClick={() => setPage(actual - 1)}
          >
            Previous
          </Button>
          <span>
            {actual + 1} / {pages}
          </span>
          <Button
            variant="outline"
            size="sm"
            disabled={actual === pages - 1}
            onClick={() => setPage(actual + 1)}
          >
            Next
          </Button>
        </div>
      </footer>
    </div>
  );
}
export function Bars({
  entries,
  label: caption,
  unit = "tokens",
}: {
  entries: RecordData[];
  label: string;
  unit?: string;
}) {
  const max = Math.max(1, ...entries.map((e) => Number(e.tokens) || 0));
  return (
    <div className="bar-chart" role="img" aria-label={caption}>
      <div className="bars">
        {entries.map((e, i) => (
          <Tip
            key={i}
            text={`${e.key}: ${e.tokens || 0} ${unit}${unit === "tokens" ? ` · ${e.active_h || 0} h active` : ""}`}
          >
            <i
              style={{
                height: Math.max(3, ((Number(e.tokens) || 0) / max) * 52),
              }}
            />
          </Tip>
        ))}
      </div>
      <p>
        {caption} · taller bars mean more {unit}
      </p>
    </div>
  );
}
