import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { FolderGit2, ShieldCheck } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import { Button } from "@/components/ui/button";
import {
  PageSection,
  Empty,
  Status,
  Text,
  Disclosure,
  Choice,
  Command,
} from "./shared";
import { list, count, time, quote, type RecordData } from "./model";
export const adoptable = (c: RecordData) =>
  c.adoptable && !/\[redacted(?: secret)?\]|\[…\]/i.test(c.path);
export function Imports({ s }: { s: RecordData }) {
  const [host, setHost] = useState<HTMLElement | null>(null);
  useEffect(() => setHost(document.getElementById("action-dock-host")), []);
  const [selected, setSelected] = useState<string[]>([]),
    [parent, setParent] = useState("root");
  const candidates = list(s.scan),
    parents = list(s.nodes).filter((n) => !n.archived),
    valid = selected.filter((path) =>
      candidates.some((c) => c.path === path && adoptable(c)),
    ),
    target = parents.some((n) => n.id === parent)
      ? parent
      : parents[0]?.id || "root";
  return (
    <div className="import-page">
      <PageSection
        title="Discovered repositories"
        description="Select repositories to prepare a command. Review it before running it."
      >
        <div className="callout">
          <ShieldCheck size={19} />
          <p>
            Discovery is read-only. Existing homes and repositories owned
            elsewhere cannot be selected.
          </p>
        </div>
        {candidates.length ? (
          candidates.map((c, i) => (
            <article className="import-entry" key={c.path}>
              <div className="entry-heading">
                <Checkbox
                  id={"candidate-" + i}
                  checked={valid.includes(c.path)}
                  disabled={!adoptable(c)}
                  aria-describedby={"reason-" + i}
                  onCheckedChange={(checked) =>
                    setSelected((old) =>
                      checked
                        ? [...old, c.path]
                        : old.filter((v) => v !== c.path),
                    )
                  }
                />
                <span className="icon-tile">
                  <FolderGit2 size={18} />
                </span>
                <div>
                  <label htmlFor={"candidate-" + i}>
                    <strong>
                      <Text>{c.name || "Repository"}</Text>
                    </strong>
                  </label>
                  <small className="break-all">{c.path}</small>
                </div>
                <Status>
                  {adoptable(c) ? "Available to adopt" : "Unavailable"}
                </Status>
              </div>
              <p className="muted">
                {c.kind || "Repository"} ·{" "}
                {count(c.commit_count || 0, "commit")} ·{" "}
                {c.last_commit_date
                  ? "Last commit " + time(c.last_commit_date, s.brain?.tz)
                  : "No commits"}{" "}
                · {(c.languages || []).join(", ") || "Languages unknown"}
              </p>
              <p className="muted">
                Agent setup: {(c.agent_setup || []).join(", ") || "None"}
                {c.agent_setup_omitted
                  ? ` · ${c.agent_setup_omitted} more`
                  : ""}
              </p>
              <p id={"reason-" + i}>
                <Text>
                  {c.ownership ||
                    (c.node
                      ? "Already a project home"
                      : adoptable(c)
                        ? "Review this repository before adoption"
                        : c.kind !== "repo"
                          ? "This is not a repository"
                          : c.parent_repo
                            ? "Nested repository or submodule"
                            : "Path unavailable")}
                </Text>
              </p>
              {c.readme && (
                <Disclosure title="Read README · untrusted source">
                  <Text>{c.readme}</Text>
                </Disclosure>
              )}
            </article>
          ))
        ) : (
          <Empty
            text="Discover your next project."
            action="Start brain ui --scan DIR to build an import checklist."
            icon={FolderGit2}
          />
        )}
      </PageSection>
      {host &&
        createPortal(
          <div className="action-dock">
            <div>
              <strong>{count(valid.length, "repository")} selected</strong>
              <p>Review the command, then run it in your brain.</p>
            </div>
            {valid.length > 0 && (
              <>
                <Choice
                  name="Parent project"
                  value={target}
                  onChange={setParent}
                  options={parents.map((n) => ({
                    value: n.id,
                    label: n.title || "Project",
                  }))}
                />
                <Command
                  text={
                    "brain adopt --parent " +
                    quote(target) +
                    " -- " +
                    [...valid].sort().map(quote).join(" ")
                  }
                />
                <Button variant="ghost" onClick={() => setSelected([])}>
                  Clear
                </Button>
              </>
            )}
          </div>,
          host,
        )}
    </div>
  );
}
