import { useId, useRef, useState, type CSSProperties } from "react";
import { Info } from "lucide-react";

export function InfoHint({ label, children }: { label: string; children: React.ReactNode }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const anchor = useRef<HTMLSpanElement>(null);
  const [position, setPosition] = useState<CSSProperties>({});
  function show() {
    const rect = anchor.current?.getBoundingClientRect();
    const bounds = anchor.current?.closest("dialog")?.getBoundingClientRect();
    const leftEdge = Math.max(0, bounds?.left || 0), rightEdge = Math.min(window.innerWidth, bounds?.right || window.innerWidth);
    if (rect) setPosition({ left: Math.max(leftEdge + 12, Math.min(rect.right - 228, rightEdge - 240)) - rect.left, right: "auto", ...(rect.bottom + 130 > Math.min(window.innerHeight, bounds?.bottom || window.innerHeight) ? { top: "auto", bottom: "calc(100% + 6px)" } : { top: "calc(100% + 6px)", bottom: "auto" }) });
    setOpen(true);
  }
  return <span ref={anchor} className={`studio-info-hint${open ? " is-open" : ""}`} onMouseEnter={show} onMouseLeave={() => setOpen(false)}>
    <button type="button" aria-label={label} aria-describedby={open ? id : undefined} onClick={(event) => { event.preventDefault(); show(); }} onFocus={show} onBlur={() => setOpen(false)} onKeyDown={(event) => { if (event.key === "Escape") setOpen(false); }}><Info size={14} /></button>
    <span id={id} role="tooltip" style={position}>{children}</span>
  </span>;
}
