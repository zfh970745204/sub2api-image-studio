import { useEffect, useState } from "react";
import { ArrowDown, ArrowUp, Copy, Eye, EyeOff, Layers, LockKeyhole, Plus, Scan, Trash2, UnlockKeyhole } from "lucide-react";
import type { RasterAction } from "./raster-editor";
import type { RasterState } from "./raster-editor.worker";
import { InfoHint } from "./InfoHint";

export function RasterLayersPanel({ state, disabled, act }: { state: RasterState; disabled: boolean; act: (action: RasterAction) => void }) {
  const active = state.layers.find((layer) => layer.id === state.activeId)!;
  const [name, setName] = useState(active.name), [opacity, setOpacity] = useState(active.opacity);
  useEffect(() => { setName(active.name); setOpacity(active.opacity); }, [active.id, active.name, active.opacity]);
  const action = (operation: Extract<RasterAction, { type: "layer" }>["operation"], id = active.id) => act({ type: "layer", operation, id });
  const rename = () => { if (name.trim() && name.trim() !== active.name) act({ type: "layer", operation: "rename", name: name.trim() }); else setName(active.name); };
  const commitOpacity = () => { if (!disabled && opacity !== active.opacity) act({ type: "layer", operation: "opacity", value: opacity }); };
  const index = state.layers.findIndex((layer) => layer.id === active.id);
  return <section className="raster-layers-panel" aria-label="图层管理">
    <div className="raster-tool-heading"><h3>图层 <span>{state.layers.length} / {state.maxLayers}</span></h3><InfoHint label="图层说明">从上到下叠放；画笔、填充和橡皮擦只修改当前图层。图层会随新版本保存，下载 PNG 为合成图。切换图层会清空选区。</InfoHint></div>
    <div className="raster-layer-actions">
      <button type="button" disabled={disabled || state.layers.length >= state.maxLayers} onClick={() => action("add")}><Plus size={15} />新建图层</button>
      <button type="button" aria-label="复制当前图层" title="复制图层" disabled={disabled || state.layers.length >= state.maxLayers} onClick={() => action("duplicate")}><Copy size={15} /></button>
      <button type="button" aria-label="删除当前图层" title="删除图层（可撤销）" disabled={disabled || state.layers.length === 1} onClick={() => action("remove")}><Trash2 size={15} /></button>
    </div>
    <div className="raster-layer-list" aria-label="图层列表">{[...state.layers].reverse().map((layer) => <div key={layer.id} className={`raster-layer-row${layer.id === active.id ? " active" : ""}${layer.visible ? "" : " hidden-layer"}`}>
      <button type="button" className="raster-layer-eye" aria-label={`${layer.visible ? "隐藏" : "显示"}图层 ${layer.name}`} title={layer.visible ? "隐藏图层" : "显示图层"} disabled={disabled} onClick={() => action("visible", layer.id)}>{layer.visible ? <Eye size={16} /> : <EyeOff size={16} />}</button>
      <button type="button" className="raster-layer-select" aria-label={`选择图层 ${layer.name}`} aria-pressed={layer.id === active.id} disabled={disabled} onClick={() => action("select", layer.id)}><span className="raster-layer-symbol"><Layers size={19} /></span><span><strong>{layer.name}</strong><small>{layer.visible ? `${layer.opacity}%` : "已隐藏"}{layer.locked ? " · 已锁定" : ""}</small></span>{layer.locked && <LockKeyhole size={13} />}</button>
    </div>)}</div>
    <label className="raster-layer-name">名称<input aria-label="图层名称" value={name} maxLength={40} disabled={disabled} onChange={(event) => setName(event.target.value)} onBlur={rename} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); event.currentTarget.blur(); } }} /></label>
    <label>图层不透明度 <output>{opacity}%</output><input aria-label="图层不透明度" type="range" min="0" max="100" value={opacity} disabled={disabled} onChange={(event) => setOpacity(Number(event.target.value))} onPointerUp={commitOpacity} onKeyUp={commitOpacity} onBlur={commitOpacity} /></label>
    <div className="raster-layer-bottom">
      <button type="button" title="图层上移" aria-label="图层上移" disabled={disabled || index === state.layers.length - 1} onClick={() => action("up")}><ArrowUp size={16} /></button>
      <button type="button" title="图层下移" aria-label="图层下移" disabled={disabled || index === 0} onClick={() => action("down")}><ArrowDown size={16} /></button>
      <button type="button" title={active.locked ? "解锁图层" : "锁定图层"} aria-label={active.locked ? "解锁图层" : "锁定图层"} aria-pressed={active.locked} disabled={disabled} onClick={() => action("locked")}>{active.locked ? <LockKeyhole size={16} /> : <UnlockKeyhole size={16} />}</button>
      <button type="button" disabled={disabled} onClick={() => action("content")}><Scan size={15} />选择内容</button>
    </div>
  </section>;
}
