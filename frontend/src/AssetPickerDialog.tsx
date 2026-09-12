import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, Images, LoaderCircle, X } from "lucide-react";
import { ImageThumbnail } from "./ImageThumbnail";
import { Pagination, useCursorPage } from "./Pagination";
import { api, type Asset } from "./user-api";
import "./asset-picker-dialog.css";

export interface AssetPickerDialogProps {
  title?: string;
  selectedId?: string;
  excludedIds?: string[];
  /** Receives the full asset on confirmation; the parent owns closing after selection. */
  onSelect?: (asset: Asset) => void;
  onSelectMany?: (assets: Asset[]) => void;
  maxSelection?: number;
  /** Called when the user dismisses the dialog without confirming. */
  onClose: () => void;
}

const kinds = [{ value: "", label: "全部素材" }, { value: "original", label: "原图" }, { value: "result", label: "处理结果" }];
const filename = (asset: Asset) => asset.original_filename || `${asset.kind === "original" ? "原图" : "处理结果"} ${asset.id.slice(0, 8)}.${asset.extension}`;
const dimensions = (asset: Asset) => asset.width && asset.height ? `${asset.width} × ${asset.height}` : "尺寸未知";

/** Mount to open. Native modality contains keyboard focus and makes the page inert. */
export function AssetPickerDialog({ title = "选择素材", selectedId, excludedIds = [], onSelect, onSelectMany, maxSelection = 50, onClose }: AssetPickerDialogProps) {
  const dialog = useRef<HTMLDialogElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const backdropPointerDown = useRef(false);
  const titleId = useId();
  const descriptionId = useId();
  const [kind, setKind] = useState("");
  const [selected, setSelected] = useState<Asset | null>(null);
  const [multiple, setMultiple] = useState<Asset[]>([]);
  const selectedMany = multiple.filter(asset => !excludedIds.includes(asset.id)).slice(0, maxSelection);
  const pager = useCursorPage(kind, (cursor, limit) => api.assets(kind, { cursor, limit, editable_only: "true" }));
  const excluded = new Set(excludedIds);
  const selection = selected && !excluded.has(selected.id) ? selected : null;

  useEffect(() => {
    const element = dialog.current!;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (typeof element.showModal === "function") element.showModal();
    else element.setAttribute("open", "");
    closeButton.current?.focus();
    return () => {
      if (typeof element.close === "function") element.close();
      if (previous?.isConnected) previous.focus();
    };
  }, []);

  useEffect(() => { setSelected(null); }, [selectedId]);
  useEffect(() => {
    // Only consume pages accepted by useCursorPage's request sequence guard.
    // Retain the full selection when paging, filtering, refreshing or retrying.
    setSelected((current) => pager.items?.find((asset) => asset.id === (current?.id ?? selectedId)) ?? current);
  }, [pager.items, selectedId]);

  return createPortal(<dialog
    ref={dialog}
    className="asset-picker-dialog"
    aria-labelledby={titleId}
    aria-describedby={descriptionId}
    aria-modal="true"
    onCancel={(event) => { event.preventDefault(); onClose(); }}
    onPointerDown={(event) => { backdropPointerDown.current = event.target === event.currentTarget; }}
    onClick={(event) => {
      if (backdropPointerDown.current && event.target === event.currentTarget) onClose();
      backdropPointerDown.current = false;
    }}
  >
    <header className="asset-picker-header">
      <div><h2 id={titleId}>{title}</h2><p id={descriptionId}>{onSelectMany ? `最多选择 ${maxSelection} 张，可跨页选择。` : "从素材库选择一张图片，确认后使用。"}</p></div>
      <button ref={closeButton} className="asset-picker-close" type="button" aria-label="关闭素材选择" onClick={onClose}><X size={20} aria-hidden="true" /></button>
    </header>
    <div className="asset-picker-toolbar">
      <div className="asset-picker-filters" role="group" aria-label="素材类型">
        {kinds.map((option) => <button key={option.value} type="button" aria-pressed={kind === option.value} onClick={() => setKind(option.value)}>{option.label}</button>)}
      </div>
      {onSelectMany ? <button type="button" onClick={() => setMultiple([...selectedMany, ...(pager.items || []).filter(asset => !excluded.has(asset.id) && !selectedMany.some(item => item.id === asset.id))].slice(0, maxSelection))} disabled={pager.loading || !pager.items?.length || selectedMany.length >= maxSelection}>选择本页</button> : <span>支持 PNG、JPG、WebP</span>}
    </div>
    <div className="asset-picker-content" aria-busy={pager.loading}>
      {pager.loading ? <div className="asset-picker-state" role="status"><LoaderCircle className="asset-picker-spinner" size={28} aria-hidden="true" /><p>正在加载素材…</p></div>
        : pager.error ? <div className="asset-picker-state" role="alert"><p>素材加载失败</p><span>{pager.error}</span><button type="button" onClick={() => void pager.load()}>重试</button></div>
          : !pager.items?.length ? <div className="asset-picker-state" role="status"><Images size={32} aria-hidden="true" /><p>暂无{kind === "original" ? "原图" : kind === "result" ? "处理结果" : "可选素材"}</p><span>可以切换类型，或上传图片后再来选择。</span></div>
            : <ul className="asset-picker-grid" aria-label="素材列表">
              {pager.items.map((asset) => {
                const unavailable = excluded.has(asset.id);
                const active = onSelectMany ? selectedMany.some(item => item.id === asset.id) : selection?.id === asset.id;
                const name = filename(asset);
                return <li key={asset.id}>
                  <button className="asset-picker-card" type="button" aria-label={`选择 ${name}${unavailable ? "（已添加）" : ""}`} aria-pressed={active} disabled={unavailable || Boolean(onSelectMany && !active && selectedMany.length >= maxSelection)} onClick={() => onSelectMany ? setMultiple(current => active ? current.filter(item => item.id !== asset.id) : [...current, asset]) : setSelected(asset)}>
                    <span className="asset-picker-thumbnail"><ImageThumbnail id={asset.id} alt={name} />
                      <span className="asset-picker-kind">{asset.kind === "original" ? "原图" : "处理结果"}</span>
                      {(active || unavailable) && <span className="asset-picker-badge"><Check size={13} aria-hidden="true" />{unavailable ? "已添加" : "已选择"}</span>}
                    </span>
                    <span className="asset-picker-details"><strong title={name}>{name}</strong><small>{dimensions(asset)}</small></span>
                  </button>
                </li>;
              })}
            </ul>}
    </div>
    <Pagination pager={pager} />
    <footer className="asset-picker-footer">
      <div className="asset-picker-selection" role="status">
        {onSelectMany ? <span>已选择 {selectedMany.length} / {maxSelection} 张图片</span> : selection ? <><span className="asset-picker-selection-image"><ImageThumbnail id={selection.id} alt="已选素材缩略图" /></span><span><strong title={filename(selection)}>已选择：{filename(selection)}</strong><small>{dimensions(selection)}</small></span></> : <span>请选择一张素材</span>}
      </div>
      <div className="asset-picker-actions"><button type="button" onClick={onClose}>取消</button><button className="asset-picker-confirm" type="button" disabled={onSelectMany ? !selectedMany.length : !selection} onClick={() => { if (onSelectMany) onSelectMany(selectedMany); else if (selection) onSelect?.(selection); }}>确认选择</button></div>
    </footer>
  </dialog>, document.body);
}
