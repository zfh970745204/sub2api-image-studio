import { AlertCircle, CheckCircle2, Info, X } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";

type ToastTone = "error" | "success" | "warning";

export function ToastMessage({ tone, children, action }: { tone: ToastTone; children: ReactNode; action?: ReactNode }) {
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    setVisible(true);
    const timer = window.setTimeout(() => setVisible(false), tone === "error" ? 6500 : 4200);
    return () => window.clearTimeout(timer);
  }, [children, tone]);

  if (!visible) return null;
  const Icon = tone === "success" ? CheckCircle2 : tone === "warning" ? Info : AlertCircle;
  return (
    <div className={`app-toast ${tone}`} role={tone === "error" ? "alert" : "status"}>
      <Icon size={17} aria-hidden="true" />
      <span>{children}</span>
      {action}
      <button type="button" aria-label="关闭提示" title="关闭提示" onClick={() => setVisible(false)}><X size={15} /></button>
    </div>
  );
}
