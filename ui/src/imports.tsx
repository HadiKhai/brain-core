import { useState } from "react";
import { FolderGit2 } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from "@/components/ui/sheet";
import { Status, Text, Command, Reader, TechnicalDetails, Empty } from "./shared";
import { RecordPicker } from "./picker";
import type { Found } from "./search";
import { list, count, time, quote, type RecordData } from "./model";

export const adoptable = (c: RecordData) => c.adoptable && !/\[redacted(?: secret)?\]|\[…\]/i.test(c.path);
const reason = (c: RecordData) => c.ownership || (c.node ? "Already a project home" : adoptable(c) ? "Review before adding" : c.kind !== "repo" ? "Not a repository" : c.parent_repo ? "Nested repository or submodule" : "Path unavailable");

// Adding projects lives with the Brain tree: a sheet over the page, its decision dock in the sheet footer.
export function AddProjects({ s, open, onOpenChange }: { s: RecordData; open: boolean; onOpenChange: (o: boolean) => void }) {
  const [selected, setSelected] = useState<string[]>([]), [previous, setPrevious] = useState<string[]>([]);
  const [parent, setParent] = useState<Found | null>(null);
  const candidates = list(s.scan), valid = selected.filter((p) => candidates.some((c) => c.path === p && adoptable(c)));
  const root = list(s.nodes).find((n) => !n.parent) || list(s.nodes)[0];
  const target = parent?.id || root?.id || "root";
  const command = "brain adopt --parent " + quote(target) + " -- " + [...valid].sort().map(quote).join(" ");
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="add-sheet" initialFocus={() => document.querySelector<HTMLElement>(".add-sheet [data-slot=sheet-close]")}>
        <SheetHeader>
          <SheetTitle>Add projects</SheetTitle>
          <SheetDescription>{candidates.length ? `${count(candidates.filter(adoptable).length, "repository")} ready to add · discovery is read-only` : "Discover local repositories, then choose what joins this brain."}</SheetDescription>
        </SheetHeader>
        <div className="sheet-body">
          {candidates.length ? <div className="rows">{candidates.map((c, i) => (
            <div className="row candidate" data-item key={c.path}>
              <Checkbox id={"candidate-" + i} checked={valid.includes(c.path)} disabled={!adoptable(c)} aria-describedby={"reason-" + i}
                onCheckedChange={(checked) => setSelected((old) => (checked ? [...old, c.path] : old.filter((v) => v !== c.path)))} />
              <div className="row-main">
                <label htmlFor={"candidate-" + i} className="row-title"><Text>{c.name || "Repository"}</Text></label>
                <small>{[count(c.commit_count || 0, "commit"), c.last_commit_date ? "last " + time(c.last_commit_date, s.brain?.tz) : "no commits", (c.languages || []).join(", ") || "languages unknown", (c.agent_setup || []).length ? "agent instructions found" : ""].filter(Boolean).join(" · ")}</small>
                <small id={"reason-" + i}><Text>{reason(c)}</Text></small>
              </div>
              <div className="row-actions">
                <Status>{adoptable(c) ? "Available" : c.already_home ? "Already in your workspace" : "Unavailable"}</Status>
                {c.readme && <Reader title={c.name || "Repository"} text={c.readme} trigger="Read notes" />}
                <TechnicalDetails values={{ Location: c.path, Setup: (c.agent_setup || []).join(", ") }} />
              </div>
            </div>
          ))}</div> : <div className="discover">
            <Empty text="No repositories discovered yet." action="Run discovery on a folder, then reopen this dashboard to choose what to add." icon={FolderGit2} />
            <Command text="brain ui --scan FOLDER" label="Copy discovery command" />
          </div>}
        </div>
        {candidates.length > 0 && <div className="sheet-dock" id="action-dock-host">
          <div className="dock-count"><strong>{count(valid.length, "repository")} selected</strong><small>Review the command, then run it in your terminal.</small></div>
          {!valid.length && candidates.some(adoptable) && !previous.length && <Button variant="outline" onClick={() => setSelected(candidates.filter(adoptable).map((c) => c.path))}>Select all available</Button>}
          {previous.length > 0 && !valid.length && <Button variant="outline" onClick={() => { setSelected(previous); setPrevious([]); }}>Undo clear</Button>}
          {valid.length > 0 && <>
            <RecordPicker s={s} kind="node" label="Parent project" value={parent} onChange={setParent} allLabel={root?.title || "Workspace root"} placeholder="Search projects" />
            <Reader title="Review import command" trigger="Review import" primary text="This adds the selected repositories under the chosen parent project. Review the command before running it in your terminal." footer={<Command text={command} label="Copy import command" />} />
            <Button variant="ghost" onClick={() => { setPrevious(selected); setSelected([]); }}>Clear</Button>
          </>}
        </div>}
      </SheetContent>
    </Sheet>
  );
}
