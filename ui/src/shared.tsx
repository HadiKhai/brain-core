import { useState, useId, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
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
import { label, readable, safeHref, type RecordData } from "./model";
export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle: string;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <div className="eyebrow">
          Workspace <ChevronRight size={12} />
          {title}
        </div>
        <h1>{title}</h1>
        <p>{subtitle}</p>
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
export function Status({ children }: { children: ReactNode }) {
  const t = String(children || "Not recorded");
  return (
    <Badge
      variant="secondary"
      className={
        "status " +
        (/near|review|stale|paused|unknown|proposed|unverified/i.test(t)
          ? "warning"
          : /blocked|stop|error|abandoned|missing/i.test(t)
            ? "danger"
            : /ready|merged|active|normal|accepted|live|complete/i.test(t)
              ? "good"
              : "")
      }
    >
      <span className="status-dot" />
      {label(t)}
    </Badge>
  );
}
export function Tip({ text, children }: { text: string; children: ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger render={<span tabIndex={0} className="tip-trigger" />}>
        {children}
      </TooltipTrigger>
      <TooltipContent className="max-w-sm break-words">{text}</TooltipContent>
    </Tooltip>
  );
}
export function Text({ children, zone }: { children: any; zone?: string }) {
  return <span className="readable">{readable(children, zone)}</span>;
}
export function Markdown({ text }: { text: any }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        components={{
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
        {readable(text)}
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
export function Command({ text }: { text: string }) {
  const [copied, setCopied] = useState("Copy");
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
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(text);
            setCopied("Copied");
          } catch {
            setCopied("Select and copy");
          }
        }}
      >
        <Copy size={14} />
        {copied}
      </Button>
    </div>
  );
}
export function Choice({
  name,
  value,
  onChange,
  options,
}: {
  name: string;
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>{name}</label>
      <Select items={options} value={value} onValueChange={(v) => onChange(String(v))}>
        <SelectTrigger id={id} aria-label={name}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map((o) => (
            <SelectItem key={o.value} value={o.value}>
              {o.label}
            </SelectItem>
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
export function DataTable({
  columns,
  rows,
  name = "records",
}: {
  columns: Column[];
  rows: RecordData[];
  name?: string;
}) {
  const [page, setPage] = useState(0),
    [size, setSize] = useState("10"),
    [sort, setSort] = useState({ key: "", direction: 1 });
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
  const pages = Math.max(1, Math.ceil(rows.length / Number(size))),
    actual = Math.min(page, pages - 1),
    shown = sorted.slice(actual * Number(size), (actual + 1) * Number(size));
  return !rows.length ? (
    <Empty text={"No " + name + " yet."} />
  ) : (
    <div className="data-table">
      <div className="mobile-table-sort">
        <Choice
          name="Sort by"
          value={sort.key || columns[0].key}
          onChange={(key) => setSort({ key, direction: 1 })}
          options={columns.map((c) => ({ value: c.key, label: c.label }))}
        />
        <Button
          variant="outline"
          size="sm"
          aria-label="Reverse sort order"
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
          </TableRow>
        </TableHeader>
        <TableBody>
          {shown.map((r, i) => (
            <TableRow key={r._key || r.id || i}>
              {columns.map((c) => (
                <TableCell
                  key={c.key}
                  data-label={c.label}
                  className={c.numeric ? "numeric" : ""}
                >
                  {c.render ? (
                    c.render(r)
                  ) : (
                    <Text>{r[c.key] ?? "Not recorded"}</Text>
                  )}
                </TableCell>
              ))}
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
            value={size}
            onChange={(v) => {
              setSize(v);
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
            text={`${e.key}: ${e.tokens || 0} ${unit} · ${e.active_h || 0} h active`}
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
