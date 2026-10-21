import { useState } from "react";
import { Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ActionLink } from "./shared";
import { safeHref } from "./model";

// Only web destinations currently have a verified opener. Keep the original
// desktop reference intact for copying into the host rather than guessing a URL.
export function ThreadLink({ href, label = "Open conversation", primary = false }: { href: any; label?: string; primary?: boolean }) {
  const [copied, setCopied] = useState(false), [failed, setFailed] = useState(false);
  const value = typeof href === "string" ? href.trim() : "";
  if (!value) return null;
  const link = safeHref(value);
  return <span className="thread-link-actions action-link-group">
    {link && <ActionLink href={link} primary={primary}>{label}</ActionLink>}
    <Button variant={primary && !link ? "default" : "ghost"} size="sm" aria-label="Copy thread link" onClick={async () => {
      try { await navigator.clipboard.writeText(value); setCopied(true); setFailed(false); }
      catch { setFailed(true); }
    }}><Copy size={14} />{copied ? "Copied thread link" : "Copy thread link"}</Button>
    {failed && <label>Copy unavailable · select the link<Input aria-label="Thread link to copy" readOnly value={value} onFocus={e => e.currentTarget.select()} /></label>}
  </span>;
}
