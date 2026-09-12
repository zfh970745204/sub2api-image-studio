import { useState, type CSSProperties, type PointerEvent, type ReactNode } from "react";
import { ImageIcon, ScanSearch } from "lucide-react";
import type { Asset } from "./user-api";

type Preview = { asset: Asset; url: string | null };
type Position = { x: number; y: number };

/** Coordinates are relative to the contained image, never to its letterboxed viewport. */
export function ComparisonPreview({ source, result, compare, empty, sourceOverlay, backgroundClass, backgroundStyle, backgroundControls, onError }: {
  source: Preview | null; result: Preview | null; compare: boolean; empty: ReactNode;
  sourceOverlay?: ReactNode; backgroundClass: string; backgroundStyle?: CSSProperties; backgroundControls?: ReactNode;
  onError: () => void;
}) {
  const [position, setPosition] = useState<Position | null>(null);
  const [zoom, setZoom] = useState(3);
  const inspectable = !sourceOverlay;
  function move(event: PointerEvent<HTMLDivElement>) {
    if (!inspectable || (event.pointerType === "touch" && !event.buttons)) return;
    const rect = event.currentTarget.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    setPosition({ x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)), y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)) });
  }
  function pane(preview: Preview | null, original: boolean) {
    const title = original ? "原图" : compare ? "处理结果" : "创作结果";
    const asset = preview?.asset;
    const ratio = (asset?.width || 1) / (asset?.height || 1);
    const magnified = inspectable && Boolean(position);
    return <article className="studio-preview-pane" aria-label={title}>
      <header><span><i className={original ? "" : "result"} />{title}</span><small>{asset ? `${asset.width ?? "—"} × ${asset.height ?? "—"}${asset.has_alpha ? " · 透明" : ""}` : original ? "保留原始文件" : "每次生成独立版本"}</small></header>
      <div className={`studio-preview-viewport ${original ? "preview-transparent" : backgroundClass}${sourceOverlay && original ? " is-painting" : ""}`} style={original ? undefined : backgroundStyle}>
        {asset ? preview?.url ? <div className={`studio-image-content${magnified ? " is-inspecting" : ""}`}
          style={{ aspectRatio: String(ratio), width: `min(100cqw, ${ratio * 100}cqh)` }}
          tabIndex={inspectable ? 0 : undefined} role={inspectable ? "group" : undefined}
          aria-label={inspectable ? `${title}细节预览，方向键移动，Escape 退出放大` : undefined}
          onPointerMove={move} onPointerDown={move} onPointerLeave={() => setPosition(null)}
          onPointerUp={(event) => { if (event.pointerType === "touch") setPosition(null); }}
          onPointerCancel={() => setPosition(null)} onBlur={() => setPosition(null)}
          onFocus={() => { if (inspectable) setPosition({ x: .5, y: .5 }); }}
          onKeyDown={(event) => {
            if (event.key === "Escape") { setPosition(null); return; }
            if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
            event.preventDefault();
            setPosition((current) => ({ x: Math.max(0, Math.min(1, (current?.x ?? .5) + (event.key === "ArrowRight" ? .05 : event.key === "ArrowLeft" ? -.05 : 0))), y: Math.max(0, Math.min(1, (current?.y ?? .5) + (event.key === "ArrowDown" ? .05 : event.key === "ArrowUp" ? -.05 : 0))) }));
          }}>
          <div className="studio-image-clip"><img draggable={false} alt={original ? "来源素材预览" : "图片任务结果"} src={preview.url} onError={onError}
            style={{ transform: magnified ? `scale(${zoom})` : undefined, transformOrigin: position ? `${position.x * 100}% ${position.y * 100}%` : undefined }} />
            {magnified && <span className="studio-inspect-cross" aria-hidden="true" style={{ left: `${position!.x * 100}%`, top: `${position!.y * 100}%` }} />}
          </div>
          {original && sourceOverlay}
        </div> : <p className="studio-result-placeholder" role="status">正在载入预览…</p>
          : original || !compare ? empty : <div className="studio-result-placeholder"><ImageIcon size={30} /><strong>等待你的下一件作品</strong><p>选好工具后开始处理，<br />结果会在这里与原图并排展示。</p></div>}
      </div>
    </article>;
  }
  return <div className="studio-preview-workspace">
    <div className={`studio-comparison${compare ? " is-dual" : ""}`} onPointerLeave={() => setPosition(null)}>
      {compare && pane(source, true)}{pane(result, false)}
    </div>
    <div className={`studio-inspection-bar${backgroundControls ? " has-background-controls" : ""}`}><span title={sourceOverlay ? "在原图上涂抹需要修改的区域" : "悬停同步放大 · 触屏按住查看 · 按相对位置对比"}><ScanSearch size={15} /><span>{sourceOverlay ? "在原图上涂抹需要修改的区域" : backgroundControls ? "悬停查看细节" : "悬停同步放大 · 触屏按住查看 · 按相对位置对比"}</span></span>
      {!sourceOverlay && <label>细节倍率<select aria-label="细节倍率" value={zoom} onChange={(event) => setZoom(Number(event.target.value))}><option value={2}>2×</option><option value={3}>3×</option><option value={4}>4×</option></select></label>}
      {backgroundControls}
    </div>
  </div>;
}
