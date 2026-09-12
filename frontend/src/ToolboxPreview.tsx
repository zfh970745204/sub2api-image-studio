import { useEffect, useRef, useState } from "react";
import { ImageThumbnail } from "./ImageThumbnail";
import { selectionPixels, type Asset } from "./user-api";
import type { ToolboxOptions } from "./toolbox-api";

export function ToolboxPreview({ asset, options }: { asset: Asset; options: ToolboxOptions }) {
  const [source, setSource] = useState<Blob | null>(null), [background, setBackground] = useState<Blob | null>(null), [watermark, setWatermark] = useState<Blob | null>(null);
  const [url, setUrl] = useState(""), [error, setError] = useState(""), [loading, setLoading] = useState(true), [retry, setRetry] = useState(0);
  const generation = useRef(0);
  useEffect(() => {
    const controller = new AbortController();
    const needsBackground = options.background === "image" && Boolean(options.background_asset_id);
    const needsWatermark = options.watermark === "image" && Boolean(options.watermark_asset_id);
    setSource(null); setBackground(null); setWatermark(null); setError(""); setLoading(true);
    void Promise.all([
      selectionPixels(`/api/v1/assets/${asset.id}/selection/result`, controller.signal),
      needsBackground ? selectionPixels(`/api/v1/assets/${options.background_asset_id}/selection/result`, controller.signal) : Promise.resolve(null),
      needsWatermark ? selectionPixels(`/api/v1/assets/${options.watermark_asset_id}/selection/result`, controller.signal) : Promise.resolve(null),
    ]).then(([nextSource, nextBackground, nextWatermark]) => {
      if (controller.signal.aborted) return;
      setSource(nextSource); setBackground(nextBackground); setWatermark(nextWatermark);
    }).catch(reason => { if (!controller.signal.aborted) { setError(reason instanceof Error ? reason.message : "预览图片读取失败，请重试"); setLoading(false); } });
    return () => controller.abort();
  }, [asset.id, options.background, options.background_asset_id, options.watermark, options.watermark_asset_id, retry]);
  useEffect(() => {
    if (!source || (options.background === "image" && !background) || (options.watermark === "image" && !watermark)) { setUrl(""); return; }
    const id = ++generation.current;
    const worker = new Worker(new URL("./toolbox-preview.worker.ts", import.meta.url), { type: "module" });
    setLoading(true); setError("");
    worker.onmessage = event => {
      if (id !== generation.current) return;
      setLoading(false);
      if (event.data.error) setError(event.data.error);
      else setUrl(URL.createObjectURL(event.data.blob));
    };
    worker.onerror = () => { setLoading(false); setError("预览暂不可用，请重新载入"); };
    const timer = window.setTimeout(() => worker.postMessage({ id, source, background, watermark, options }), 100);
    return () => { ++generation.current; window.clearTimeout(timer); worker.terminate(); };
  }, [source, background, watermark, options]);
  useEffect(() => () => { if (url) URL.revokeObjectURL(url); }, [url]);
  return <div className="toolbox-comparison"><figure><figcaption>原图 <span>{asset.width} × {asset.height}</span></figcaption><div><ImageThumbnail id={asset.id} alt="工具箱原图" /></div></figure><figure><figcaption>布局预览 <span>{loading ? "更新中…" : "导出前预览"}</span></figcaption><div aria-busy={loading}>{error ? <p role="alert">{error}<button type="button" onClick={() => setRetry(value => value + 1)}>重新载入</button></p> : url ? <img src={url} alt="工具箱布局预览" /> : <span>正在载入预览…</span>}</div></figure><small>预览用于检查布局；最终清晰度、裁边尺寸和文件大小以处理结果为准。</small></div>;
}
