import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowRight, Check, Download, FileImage, Images, Layers, LoaderCircle, Maximize, Palette, RefreshCw, RotateCw, SlidersHorizontal, Upload, X } from "lucide-react";
import { api, ApiError, type Asset, type BootstrapData, type ImageJob } from "./user-api";
import { AssetPickerDialog } from "./AssetPickerDialog";
import { ImageThumbnail } from "./ImageThumbnail";
import { ToolboxPreview } from "./ToolboxPreview";
import { defaultToolboxOptions, downloadBundle, processingSteps, toolboxApi, toolboxDimensions, type ToolboxOptions, type ToolboxQuote, type ToolboxParameters } from "./toolbox-api";
import { usePageVisible } from "./usePageVisible";
import "./toolbox.css";

const tools = [
  { id: "compress", label: "图片压缩", detail: "按质量或目标 KB", icon: SlidersHorizontal },
  { id: "format", label: "格式转换", detail: "PNG / JPG / WebP", icon: FileImage },
  { id: "resize", label: "尺寸与画布", detail: "缩放、裁边与留白", icon: Maximize },
  { id: "rotate", label: "旋转翻转", detail: "调整方向与镜像", icon: RotateCw },
  { id: "background", label: "背景合成", detail: "透明、纯色或图片", icon: Palette },
  { id: "batch", label: "批量处理", detail: "组合步骤，整组导出", icon: Layers },
] as const;
const pending = (job: ImageJob) => ["queued", "running", "retry_wait"].includes(job.status);
const failed = (job: ImageJob) => ["failed", "timed_out", "cancelled"].includes(job.status);
const stateName: Record<string, string> = { queued: "排队中", running: "处理中", retry_wait: "等待重试", succeeded: "已完成", failed: "失败", timed_out: "超时", cancelled: "已取消" };
const size = (bytes: number) => bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 / 1024).toFixed(2)} MB`;
const errorText = (error: unknown) => error instanceof Error ? error.message : "操作失败，请重试";
function go(url: string) { window.history.pushState({}, "", url); window.dispatchEvent(new PopStateEvent("popstate")); }
function clearDraft(key: string) { try { sessionStorage.removeItem(key); } catch { /* Storage may be unavailable. */ } }
function validDraft(value: unknown): value is ToolboxQuote {
  if (!value || typeof value !== "object") return false;
  const draft = value as ToolboxQuote;
  return typeof draft.batch_id === "string" && Number.isFinite(draft.total_points)
    && Array.isArray(draft.items) && draft.items.length > 0 && draft.items.length <= 50
    && draft.items.every(item => item?.quote?.id && typeof item.parameters?.output_name === "string"
      && typeof item.parameters?.options?.format === "string" && item.parameters.batch_id === draft.batch_id);
}

export function ToolboxPage({ bootstrap, onBootstrap }: { bootstrap: BootstrapData; onBootstrap: (data: BootstrapData) => void }) {
  const [active, setActive] = useState<string>("compress");
  const [options, setOptions] = useState<ToolboxOptions>({ ...defaultToolboxOptions, format: "webp" });
  const [assets, setAssets] = useState<Asset[]>([]), [focused, setFocused] = useState("");
  const [picker, setPicker] = useState<"source" | "background" | null>(null);
  const [backgroundAsset, setBackgroundAsset] = useState<Asset | null>(null);
  const [batchName, setBatchName] = useState("图片处理"), [prefix, setPrefix] = useState("");
  const [busy, setBusy] = useState(""), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [jobs, setJobs] = useState<ImageJob[]>([]), [jobError, setJobError] = useState("");
  const [batchFilter, setBatchFilter] = useState(new URLSearchParams(window.location.search).get("batch") || "");
  const [quote, setQuote] = useState<ToolboxQuote | null>(null), [submitUncertain, setSubmitUncertain] = useState(false);
  const [result, setResult] = useState<{ asset: Asset; url: string } | null>(null);
  const [available, setAvailable] = useState(false);
  const uploadRef = useRef<HTMLInputElement>(null), backgroundRef = useRef<HTMLInputElement>(null), guard = useRef(false);
  const visible = usePageVisible(), refreshSequence = useRef(0);
  const storageKey = `toolbox-pending:${bootstrap.user.id}`;
  const source = assets.find(asset => asset.id === focused) || assets[0];
  const bootstrapRef = useRef(bootstrap); bootstrapRef.current = bootstrap;
  const change = (patch: Partial<ToolboxOptions>) => { setOptions(current => ({ ...current, ...patch })); setResult(null); setError(""); };
  const canCreate = bootstrap.permissions.includes("studio.use") && bootstrap.permissions.includes("tasks.create");
  const canUpload = bootstrap.permissions.includes("assets.write_own");
  const maxMp = Math.min(16, bootstrap.membership.entitlements.max_image_megapixels || 16);

  const refreshJobs = useCallback(async () => {
    const sequence = ++refreshSequence.current;
    try {
      const latest = new Map<string, ImageJob>();
      let cursor: string | null = null;
      do {
        const data = await api.jobs("", { operation_code: "image.toolbox", batch_id: batchFilter, limit: 50, cursor });
        if (sequence !== refreshSequence.current) return;
        for (const job of data.items) {
          const key = batchFilter ? `${job.source_asset_id}:${job.parameters.output_name}` : job.id;
          if (!latest.has(key)) latest.set(key, job);
        }
        cursor = data.next_cursor;
      } while (batchFilter && cursor && latest.size < 50);
      setJobs([...latest.values()].slice(0, 50)); setJobError("");
    }
    catch (reason) { if (sequence === refreshSequence.current) setJobError(errorText(reason)); }
  }, [batchFilter]);
  useEffect(() => {
    let alive = true;
    void api.operations().then(data => { if (alive) setAvailable(data.items.some(item => item.code === "image.toolbox" && item.enabled)); }).catch(reason => { if (alive) setError(errorText(reason)); });
    try { const saved = sessionStorage.getItem(storageKey); if (saved) { const draft = JSON.parse(saved); if (validDraft(draft)) { setQuote(draft); setSubmitUncertain(true); } else clearDraft(storageKey); } } catch { clearDraft(storageKey); }
    const params = new URLSearchParams(window.location.search), id = params.get("source"), jobId = params.get("job");
    if (id) void api.asset(id).then(data => { if (alive) { setAssets([data.asset]); setFocused(id); } }).catch(reason => { if (alive) setError(errorText(reason)); });
    if (jobId) void api.job(jobId).then(async ({ job }) => {
      if (!alive || job.operation_code !== "image.toolbox") return;
      const settings = job.parameters as unknown as ToolboxParameters;
      setOptions(settings.options); setBatchName(settings.batch_name); setBatchFilter(settings.batch_id); setActive("batch");
      if (settings.options.background_asset_id) void api.asset(settings.options.background_asset_id).then(({ asset }) => { if (alive) setBackgroundAsset(asset); }).catch(reason => { if (alive) setError(errorText(reason)); });
      if (job.source_asset_id) { const { asset } = await api.asset(job.source_asset_id); if (alive) { setAssets([asset]); setFocused(asset.id); } }
    }).catch(reason => { if (alive) setError(errorText(reason)); });
    return () => { alive = false; ++refreshSequence.current; };
  }, [storageKey]);
  useEffect(() => { void refreshJobs(); }, [refreshJobs]);
  const hasPending = jobs.some(pending);
  useEffect(() => {
    if (!visible || !hasPending) return;
    let stopped = false;
    const poll = async () => { await refreshJobs(); if (!stopped) timer = window.setTimeout(poll, 3000); };
    let timer = window.setTimeout(poll, 3000);
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [hasPending, visible, refreshJobs]);
  useEffect(() => {
    if (!busy) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [busy]);

  function addAssets(selected: Asset[]) {
    setAssets(current => [...current, ...selected.filter(asset => !current.some(item => item.id === asset.id))].slice(0, 50));
    if (!source && selected[0]) setFocused(selected[0].id);
    setResult(null); setPicker(null);
  }
  function continueResult(asset: Asset) {
    if (!assets.some(item => item.id === asset.id) && assets.length >= 50) {
      setError("当前已选满 50 张，请先移除一张图片再继续处理此结果。");
      return;
    }
    addAssets([asset]); setFocused(asset.id);
  }
  async function upload(files: File[], background = false) {
    if (guard.current || !files.length) return;
    const list = files.slice(0, background ? 1 : 50 - assets.length);
    if (!list.length) { setError("每批最多 50 张图片"); return; }
    guard.current = true; setBusy("上传中"); setError(""); const errors: string[] = [], added: Asset[] = [];
    try {
      for (let i = 0; i < list.length; i++) {
        setBusy(`上传 ${i + 1} / ${list.length}`);
        try {
          if (list[i].size > bootstrap.membership.entitlements.max_upload_mb * 1024 * 1024) throw new Error(`超过 ${bootstrap.membership.entitlements.max_upload_mb} MB`);
          const { asset } = await api.uploadAsset(list[i]);
          if (!asset.width || !asset.height || asset.width * asset.height > maxMp * 1_000_000) throw new Error(`超过 ${maxMp} 百万像素，已上传素材库`);
          added.push(asset);
        } catch (reason) { errors.push(`${list[i].name}：${errorText(reason)}`); }
      }
      if (background && added[0]) { setBackgroundAsset(added[0]); change({ background: "image", background_asset_id: added[0].id }); }
      else addAssets(added);
      if (errors.length) setError(errors.join("；"));
      if (files.length > list.length) setNotice(`本次只添加前 ${list.length} 张，每批最多 50 张。`);
    } finally { guard.current = false; setBusy(""); }
  }
  async function prepare(retryJobs?: ImageJob[]) {
    if (guard.current || quote) return;
    guard.current = true; setBusy("核算中"); setError("");
    try {
      let prepared: ToolboxQuote;
      if (retryJobs?.length) {
        const items: ToolboxQuote["items"] = [];
        for (const job of retryJobs) { const parameters = job.parameters as unknown as ToolboxParameters; const response = await api.quote("image.toolbox", job.source_asset_id, parameters as unknown as Record<string, unknown>); items.push({ source_asset_id: job.source_asset_id!, parameters, quote: response.quote }); }
        prepared = { batch_id: items[0].parameters.batch_id, total_points: items.reduce((sum, item) => sum + item.quote.final_points, 0), items };
      } else prepared = await toolboxApi.quote(assets.map(asset => asset.id), options, batchName.trim() || "图片处理", prefix);
      setSubmitUncertain(false); setQuote(prepared);
    } catch (reason) { setError(errorText(reason)); }
    finally { guard.current = false; setBusy(""); }
  }
  async function submit() {
    if (!quote || guard.current) return;
    guard.current = true; setBusy("提交中"); setError("");
    // Retain the exact quote IDs until the server acknowledges the transaction.
    try { sessionStorage.setItem(storageKey, JSON.stringify(quote)); } catch { /* Server idempotency still covers retries in this page. */ }
    try {
      const data = await toolboxApi.submit(quote);
      setBatchFilter(quote.batch_id); setJobs(data.items); ++refreshSequence.current;
      setNotice(`已提交 ${data.items.length} 张图片，后台按并发上限处理；你可以继续选择图片或离开本页。`);
      setQuote(null); setSubmitUncertain(false); clearDraft(storageKey);
      void api.pointBalance().then(data => onBootstrap({ ...bootstrapRef.current, points: { ...bootstrapRef.current.points, ...data.account } })).catch(() => undefined);
      if (quote.batch_id === batchFilter) void refreshJobs();
    } catch (reason) {
      const uncertain = !(reason instanceof ApiError) || reason.status >= 500;
      setSubmitUncertain(uncertain);
      if (!uncertain) clearDraft(storageKey);
      setError(errorText(reason));
    } finally { guard.current = false; setBusy(""); }
  }
  async function showResult(job: ImageJob) {
    if (!job.output_asset_id) return;
    try { const [{ asset }, data] = await Promise.all([api.asset(job.output_asset_id), api.downloadUrl(job.output_asset_id)]); setResult({ asset, url: data.url }); }
    catch (reason) { setError(errorText(reason)); }
  }
  async function download(ids: string[], bundle = true) {
    if (guard.current) return;
    guard.current = true; setBusy(bundle ? "打包中" : "准备下载"); setError("");
    try {
      if (bundle) await downloadBundle(ids);
      else { const { url } = await api.downloadUrl(ids[0]); const link = document.createElement("a"); link.href = url; link.rel = "noopener"; link.click(); }
    } catch (reason) { setError(errorText(reason)); }
    finally { guard.current = false; setBusy(""); }
  }
  const successful = jobs.filter(job => job.status === "succeeded" && job.output_asset_id);
  const activeCount = jobs.filter(pending).length;
  const dimensions = source && !options.trim ? toolboxDimensions(source.width || 1, source.height || 1, options) : null;
  const invalidSize = dimensions && (dimensions[0] * dimensions[1] > maxMp * 1_000_000 || Math.max(...dimensions) > 12000);
  const number = (key: "width" | "height" | "percent" | "padding" | "quality" | "target_kb", label: string, min: number, max: number) => <label className="toolbox-field">{label}<input type="number" min={min} max={max} value={options[key]} onChange={event => { if (Number.isFinite(event.target.valueAsNumber)) change({ [key]: Math.round(event.target.valueAsNumber) }); }} /></label>;
  if (!canCreate) return <p role="alert">当前账号没有图片工具箱使用权限。</p>;

  return <div className="toolbox-page">
    <header className="toolbox-title"><div><h1>图片工具箱</h1><p>从一张到一整组，把常用处理一次完成。</p></div><button className="user-secondary" onClick={() => go("/app/studio")} type="button">AI 图片编辑器<ArrowRight size={16} /></button></header>
    <nav className="toolbox-tools" aria-label="基础图片工具">{tools.map(tool => <button key={tool.id} type="button" aria-pressed={active === tool.id} onClick={() => { setActive(tool.id); if (tool.id === "compress" && options.format === "png") change({ format: "webp" }); if (tool.id === "resize" && options.resize === "original") change({ resize: "fit" }); }}><tool.icon size={20} /><span><strong>{tool.label}</strong><small>{tool.detail}</small></span></button>)}</nav>
    {error && !quote && <p className="toolbox-error" role="alert">{error}</p>}{notice && <p className="toolbox-notice" role="status">{notice}</p>}
    <div className="toolbox-layout">
      <section className="toolbox-workspace" onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); if (canUpload) void upload(Array.from(event.dataTransfer.files)); }}>
        <header><strong>处理图片 <small>{assets.length} / 50</small></strong><div>{assets.length > 0 && <button className="user-secondary" type="button" disabled={Boolean(busy)} onClick={() => { setAssets([]); setFocused(""); setResult(null); }}>清空所选</button>}<button className="user-secondary" type="button" disabled={Boolean(busy) || assets.length >= 50} onClick={() => setPicker("source")}><Images size={15} />素材库</button><button className="user-secondary" type="button" disabled={!canUpload || Boolean(busy) || assets.length >= 50} onClick={() => uploadRef.current?.click()}><Upload size={15} />上传图片</button></div></header>
        <input ref={uploadRef} type="file" accept="image/png,image/jpeg,image/webp" multiple hidden onChange={event => { void upload(Array.from(event.target.files || [])); event.currentTarget.value = ""; }} />
        <input ref={backgroundRef} type="file" accept="image/png,image/jpeg,image/webp" hidden onChange={event => { void upload(Array.from(event.target.files || []), true); event.currentTarget.value = ""; }} />
        {assets.length > 0 && <div className="toolbox-source-list" aria-label="本批来源图片">{assets.map(asset => <div key={asset.id}><button type="button" aria-pressed={source?.id === asset.id && !result} onClick={() => { setFocused(asset.id); setResult(null); }}><ImageThumbnail id={asset.id} /><span title={asset.original_filename || asset.id}>{asset.original_filename || "图片"}</span></button><button aria-label={`移除 ${asset.original_filename || asset.id}`} type="button" disabled={Boolean(busy)} onClick={() => { setAssets(current => current.filter(item => item.id !== asset.id)); setResult(null); }}><X size={12} /></button></div>)}</div>}
        {result ? <div className="toolbox-result-preview"><header><strong>{result.asset.original_filename}</strong><span>{result.asset.width} × {result.asset.height} · {size(result.asset.size_bytes)}</span></header><img src={result.url} alt="工具箱处理结果" /><footer><button type="button" className="user-secondary" onClick={() => continueResult(result.asset)}>继续处理此结果</button><button type="button" className="user-secondary" onClick={() => go(`/app/studio?source=${result.asset.id}`)}>在图片编辑器打开</button></footer></div>
          : source ? <ToolboxPreview key={source.id} asset={source} options={options} /> : <div className="toolbox-empty"><span><Images size={36} /></span><h2>添加图片，开始处理</h2><p>拖入图片，或从素材库跨页多选。<br />同一套设置应用于全部图片。</p><small>PNG / JPG / WebP · 每批最多 50 张 · 最高 {maxMp} 百万像素</small></div>}
        <div className="toolbox-flow"><span>本次处理流程</span><strong>{processingSteps(options)}</strong></div>
      </section>
      <aside className="toolbox-settings"><header><h2>{tools.find(tool => tool.id === active)?.label}</h2><button type="button" title="重置全部参数" aria-label="重置全部参数" onClick={() => { setOptions({ ...defaultToolboxOptions }); setResult(null); }}><RefreshCw size={16} /></button></header>
        <fieldset disabled={Boolean(busy) || Boolean(quote)}>
          {active === "resize" && <section className="toolbox-setting-section"><label className="toolbox-field">缩放方式<select value={options.resize} onChange={event => change({ resize: event.target.value as ToolboxOptions["resize"] })}><option value="original">保持原尺寸</option><option value="fit">等比适应（完整保留）</option><option value="fill">铺满尺寸（居中裁切）</option><option value="stretch">拉伸到指定尺寸</option><option value="percent">按百分比缩放</option></select></label>{options.resize === "percent" ? number("percent", "缩放比例 %", 1, 800) : options.resize !== "original" && <><label className="toolbox-field">尺寸预设<select aria-label="尺寸预设" defaultValue="" onChange={event => { if (event.target.value) { const [width, height] = event.target.value.split("x").map(Number); change({ width, height }); } }}><option value="">自定义尺寸</option><option value="2000x2000">商品方图 · 2000 × 2000</option><option value="1080x1440">竖版配图 · 1080 × 1440</option><option value="1920x1080">横版封面 · 1920 × 1080</option><option value="1080x1920">竖屏封面 · 1080 × 1920</option></select></label><div className="toolbox-pair">{number("width", "宽度 px", 1, 12000)}{number("height", "高度 px", 1, 12000)}</div></>}
          <label className="toolbox-check"><input type="checkbox" checked={options.trim} onChange={event => change({ trim: event.target.checked })} />先自动去除透明边缘</label>{number("padding", "四周留白 px", 0, 2000)}</section>}
          {active === "rotate" && <section className="toolbox-setting-section"><label className="toolbox-field">顺时针旋转<select value={options.rotation} onChange={event => change({ rotation: Number(event.target.value) as ToolboxOptions["rotation"] })}>{[0, 90, 180, 270].map(value => <option key={value} value={value}>{value}°</option>)}</select></label><div className="toolbox-toggle-row"><button type="button" aria-pressed={options.flip_horizontal} onClick={() => change({ flip_horizontal: !options.flip_horizontal })}>水平翻转</button><button type="button" aria-pressed={options.flip_vertical} onClick={() => change({ flip_vertical: !options.flip_vertical })}>垂直翻转</button></div><p>翻转在旋转之后进行。</p></section>}
          {active === "background" && <section className="toolbox-setting-section"><label className="toolbox-field">背景类型<select value={options.background} onChange={event => change({ background: event.target.value as ToolboxOptions["background"] })}><option value="transparent">保持透明</option><option value="color">纯色背景</option><option value="image">图片背景</option></select></label>{options.background === "color" && <label className="toolbox-color">背景颜色<input aria-label="背景颜色" type="color" value={options.color} onChange={event => change({ color: event.target.value })} /><span>{options.color.toUpperCase()}</span></label>}{options.background === "image" && <div className="toolbox-background-picker">{backgroundAsset && <ImageThumbnail id={backgroundAsset.id} alt="已选背景图" />}<button className="user-secondary" type="button" onClick={() => setPicker("background")}>{options.background_asset_id ? "更换背景图" : "从素材库选择背景图"}</button><button className="user-secondary" type="button" disabled={!canUpload} onClick={() => backgroundRef.current?.click()}>上传背景图</button></div>}{number("padding", "四周留白 px", 0, 2000)}<p>背景会合成到导出文件。主体需有透明区域才能显示新背景，可先到图片编辑器抠图。</p></section>}
          {active === "batch" && <section className="toolbox-setting-section"><label className="toolbox-field">批次名称<input maxLength={80} value={batchName} onChange={event => setBatchName(event.target.value)} /></label><label className="toolbox-field">统一文件名前缀<input maxLength={80} value={prefix} placeholder="留空使用原文件名" onChange={event => setPrefix(event.target.value)} /></label><p>切换上方工具组合步骤，设置会保留。每张图独立排队，失败后可以单独重试。</p></section>}
          <details className="toolbox-export-panel" key={active} open={["compress", "format", "batch"].includes(active)}><summary>输出设置<span>{options.format.toUpperCase()} · {options.format === "png" ? "无损" : options.compression === "target" ? `${options.target_kb} KB` : `质量 ${options.quality}`}</span></summary><section className="toolbox-setting-section toolbox-export"><label className="toolbox-field">输出格式<select value={options.format} onChange={event => change({ format: event.target.value as ToolboxOptions["format"], ...(event.target.value === "png" ? { compression: "quality" } : {}) })}><option value="png">PNG · 支持透明</option><option value="jpg">JPG · 不透明</option><option value="webp">WebP · 支持透明</option></select></label>{options.format === "png" ? <p>PNG 使用无损压缩，保持透明度和像素颜色。</p> : <><label className="toolbox-field">压缩方式<select value={options.compression} onChange={event => change({ compression: event.target.value as ToolboxOptions["compression"] })}><option value="quality">按图片质量</option><option value="target">按目标文件大小</option></select></label>{options.compression === "target" ? number("target_kb", "目标大小 KB", 1, 20480) : <label className="toolbox-field toolbox-quality"><span>图片质量</span><strong>{options.quality}</strong><input aria-label="图片质量" type="range" min={1} max={100} value={options.quality} onChange={event => change({ quality: Number(event.target.value) })} /></label>}{options.compression === "target" && <p>通过调整编码质量尽量达到目标，保留图片尺寸；无法达到时会明确提示。</p>}</>}{options.format === "jpg" && options.background === "transparent" && <label className="toolbox-color">透明区域填充<input aria-label="JPG 底色" type="color" value={options.color} onChange={event => change({ color: event.target.value })} /><span>{options.color.toUpperCase()}</span></label>}</section></details>
        </fieldset>
        <footer>{dimensions && <span className={invalidSize ? "toolbox-error" : ""}>预计尺寸 {dimensions[0]} × {dimensions[1]} px{invalidSize && " · 超过像素限制"}</span>}<button className="user-primary" type="button" disabled={!assets.length || Boolean(busy) || Boolean(quote) || !available || Boolean(invalidSize) || (options.background === "image" && !options.background_asset_id)} onClick={() => void prepare()}>{busy ? <LoaderCircle className="spin" size={17} /> : <Check size={17} />}{busy || `处理 ${assets.length || "所选"} 张图片`}</button><small>{available ? "确认报价后提交 · 原图保留 · 后台排队处理" : "基础图片处理暂未启用，请联系管理员"}</small></footer>
      </aside>
    </div>
    <section className="toolbox-history"><header><div><h2>{batchFilter ? "批次任务" : "近期任务"}</h2><span>{activeCount ? `${activeCount} 张排队或处理中` : `${successful.length} 张已完成`} · 刷新或离开页面后任务仍继续</span></div><div>{batchFilter && <button type="button" className="user-secondary" onClick={() => setBatchFilter("")}>全部近期任务</button>}<button type="button" className="user-secondary" onClick={() => void refreshJobs()}><RefreshCw size={14} />刷新</button><button type="button" className="user-secondary" disabled={!successful.length || Boolean(busy)} onClick={() => void download(successful.map(job => job.output_asset_id!))}><Download size={14} />打包已完成 ({successful.length})</button></div></header>
      {jobError && <p role="alert" className="toolbox-error">{jobError}</p>}
      {!jobs.length ? <p className="toolbox-history-empty">提交后可在这里查看每张图片的进度和结果。</p> : <div className="toolbox-job-list">{jobs.map(job => <article key={job.id}><ImageThumbnail id={job.output_asset_id || job.source_asset_id} /><div><strong>{String(job.parameters.output_name || "图片处理")}</strong><button className="toolbox-batch-link" type="button" onClick={() => setBatchFilter(String(job.parameters.batch_id))}>{String(job.parameters.batch_name || "批次")}</button>{job.error_message && <small className="toolbox-error">{job.error_message}</small>}</div><span className={`toolbox-job-state ${job.status}`}>{stateName[job.status] || job.status}{pending(job) && <progress max={100} value={job.progress} />}</span><div className="toolbox-job-actions">{job.output_asset_id && <><button type="button" onClick={() => void showResult(job)}>查看结果</button><button type="button" disabled={Boolean(busy)} onClick={() => void download([job.output_asset_id!], false)}>下载</button></>}{failed(job) && <button type="button" disabled={Boolean(busy) || Boolean(quote)} onClick={() => void prepare([job])}>重试此图</button>}{job.status === "queued" && bootstrap.permissions.includes("tasks.cancel_own") && <button type="button" onClick={() => void api.cancelJob(job.id).then(refreshJobs).catch(reason => setError(errorText(reason)))}>取消排队</button>}</div></article>)}</div>}
      <footer><span>显示最近 50 项；全部记录可在任务中心查看。</span><button type="button" onClick={() => go("/app/jobs")}>任务中心<ArrowRight size={14} /></button></footer>
    </section>
    {picker === "source" && <AssetPickerDialog title="批量选择图片" excludedIds={assets.map(asset => asset.id)} maxSelection={50 - assets.length} onSelectMany={addAssets} onClose={() => setPicker(null)} />}
    {picker === "background" && <AssetPickerDialog title="选择背景图片" onSelect={asset => { setBackgroundAsset(asset); change({ background_asset_id: asset.id, background: "image" }); setPicker(null); }} onClose={() => setPicker(null)} />}
    {quote && <ToolboxConfirmation quote={quote} busy={busy === "提交中"} error={error} uncertain={submitUncertain} onSubmit={() => void submit()} onCancel={() => { setQuote(null); setError(""); if (!submitUncertain) clearDraft(storageKey); }} />}
  </div>;
}

function ToolboxConfirmation({ quote, busy, error, uncertain, onSubmit, onCancel }: { quote: ToolboxQuote; busy: boolean; error: string; uncertain: boolean; onSubmit: () => void; onCancel: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const node = dialog.current!; const previous = document.activeElement as HTMLElement | null; if (node.showModal) node.showModal(); else node.setAttribute("open", ""); return () => { node.close?.(); previous?.focus(); }; }, []);
  return createPortal(<dialog ref={dialog} className="toolbox-confirm" aria-modal="true" aria-labelledby="toolbox-confirm-title" onCancel={event => { event.preventDefault(); if (!busy && !uncertain) onCancel(); }}><h2 id="toolbox-confirm-title">确认批量处理</h2><p>{quote.items.length} 张图片 · 共 <strong>{quote.total_points} 积分</strong></p><div className="toolbox-confirm-flow">{processingSteps(quote.items[0].parameters.options)}</div><ul>{quote.items.map(item => <li key={item.quote.id}>{item.parameters.output_name}<span>{item.quote.final_points} 积分</span></li>)}</ul><p>生成独立版本，原图保留。按账号并发上限执行，多余任务排队。</p>{error && <p role="alert" className="toolbox-error">{error}</p>}{uncertain && <p>上次提交尚未确认，请重试提交以核对结果；重复提交同一报价不会重复扣费或创建任务。</p>}<footer><button type="button" className="user-secondary" disabled={busy || uncertain} onClick={onCancel}>取消</button><button type="button" className="user-primary" disabled={busy} onClick={onSubmit}>{busy ? "正在提交…" : uncertain ? "重试提交" : "确认提交"}</button></footer></dialog>, document.querySelector(".user-shell") || document.body);
}
