import { useState, type ReactNode } from "react";
import { DetailRow, Reader } from "./shared";

export function ReaderRow({ title, text, trigger = "Read", children, className = "row", footer }: { title: string; text: string; trigger?: string; children: ReactNode; className?: string; footer?: ReactNode }) {
  const [open, setOpen] = useState(false);
  return <DetailRow className={className} onOpen={() => setOpen(true)}>{children}<div className="row-actions"><Reader title={title} text={text} trigger={trigger} open={open} onOpenChange={setOpen} footer={footer} /></div></DetailRow>;
}
