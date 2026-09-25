import {
  AlertTriangle,
  Check,
  Crop,
  Download,
  Eraser,
  History,
  ImagePlus,
  LoaderCircle,
  ScanLine,
  ShieldCheck,
  Sparkles,
  Upload,
  WandSparkles,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  getAssets,
  getCapabilities,
  getLineage,
  runPreflight,
  runWorkflow,
  uploadAsset,
} from "./api";
import type {
  Capabilities,
  CropRect,
  ImageQuality,
  ImageResult,
  ImageSize,
  PreflightReport,
  RestoreMode,
  WorkflowToolId,
} from "./types";

const workflowTools = [
  { id: "extract-print" as const, label: "提取印花", short: "提图", icon: Crop },
  { id: "restore" as const, label: "高清修复", short: "高清", icon: ScanLine },
  { id: "remove-background" as const, label: "主体抠图", short: "抠图", icon: Eraser },
  { id: "ai-reconstruct" as const, label: "AI 重建", short: "重建", icon: WandSparkles },
  { id: "generate" as const, label: "图片生成", short: "生成", icon: Sparkles },
];

const operationLabels: Record<string, string> = {
  upload: "原始素材",
  generate: "生成",
  edit: "AI 重建",
  "extract-print": "印花提取",
  restore: "高清修复",
  "remove-background": "主体抠图",
  upscale: "兼容放大",
};

const defaultCrop: CropRect = { x: 0.2, y: 0.2, width: 0.6, height: 0.6 };

function PodApp() {
  const [tool, setTool] = useState<WorkflowToolId>("extract-print");
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [assets, setAssets] = useState<ImageResult[]>([]);
  const [source, setSource] = useState<ImageResult | null>(null);
  const [result, setResult] = useState<ImageResult | null>(null);
  const [compareSource, setCompareSource] = useState<ImageResult | null>(null);
  const [lineage, setLineage] = useState<ImageResult[]>([]);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [crop, setCrop] = useState<CropRect>(defaultCrop);
  const [restoreMode, setRestoreMode] = useState<RestoreMode>("faithful");
  const [scale, setScale] = useState<2 | 4>(4);
  const [denoise, setDenoise] = useState(35);
  const [deblur, setDeblur] = useState(35);
  const [backgroundTolerance, setBackgroundTolerance] = useState(35);
  const [textureReduction, setTextureReduction] = useState(45);
  const [shadowReduction, setShadowReduction] = useState(55);
  const [edgeCleanup, setEdgeCleanup] = useState(35);
  const [prompt, setPrompt] = useState("");
  const [size, setSize] = useState<ImageSize>("auto");
  const [quality, setQuality] = useState<ImageQuality>("high");
  const [targetWidthCm, setTargetWidthCm] = useState(30);
  const [targetDpi, setTargetDpi] = useState(300);
  const [preflight, setPreflight] = useState<PreflightReport | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const selectedTool = useMemo(
    () => workflowTools.find((item) => item.id === tool)!,
    [tool],
  );
  const displayed = result ?? source;
  const needsSource = tool !== "generate";
  const needsPrompt = tool === "generate" || tool === "ai-reconstruct";
  const canRun =
    !busy &&
    (!needsSource || Boolean(source)) &&
    (!needsPrompt || Boolean(prompt.trim())) &&
    (!(tool === "generate" || tool === "ai-reconstruct") || capabilities?.sub2api_configured !== false);

  useEffect(() => {
    Promise.all([getCapabilities(), getAssets()])
      .then(([nextCapabilities, nextAssets]) => {
        setCapabilities(nextCapabilities);
        setAssets(nextAssets);
        if (nextAssets.length) setSource(nextAssets[0]);
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "服务连接失败"));
  }, []);

  useEffect(() => {
    setPreflight(null);
    if (!displayed?.root_id) {
      setLineage([]);
      return;
    }
    getLineage(displayed.id).then(setLineage).catch(() => setLineage([]));
  }, [displayed?.id, displayed?.root_id]);

  function selectTool(nextTool: WorkflowToolId) {
    if (result) setSource(result);
    setTool(nextTool);
    setResult(null);
    setCompareSource(null);
    setError(null);
  }

  async function acceptFile(nextFile?: File) {
    if (!nextFile) return;
    if (!nextFile.type.startsWith("image/")) {
      setError("请选择 PNG、JPEG 或 WebP 图片");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const uploaded = await uploadAsset(nextFile);
      setAssets((items) => [uploaded, ...items.filter((item) => item.id !== uploaded.id)]);
      setSource(uploaded);
      setResult(null);
      setCompareSource(null);
      setCrop(defaultCrop);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "上传失败");
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function submit() {
    if (!canRun) return;
    setBusy(true);
    setError(null);
    setPreflight(null);
    try {
      const next = await runWorkflow({
        tool,
        sourceAssetId: source?.id ?? null,
        prompt: prompt.trim(),
        size,
        quality,
        restoreMode,
        scale,
        denoise,
        deblur,
        crop,
        backgroundTolerance,
        textureReduction,
        shadowReduction,
        edgeCleanup,
      });
      setCompareSource(source);
      setResult(next);
      setAssets((items) => [next, ...items.filter((item) => item.id !== next.id)]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "处理失败，请重试");
    } finally {
      setBusy(false);
    }
  }

  function useAsset(asset: ImageResult) {
    setSource(asset);
    setResult(null);
    setCompareSource(null);
    setPreflight(null);
    setError(null);
  }

  function continueWith(nextTool: WorkflowToolId) {
    if (!displayed) return;
    setSource(displayed);
    setResult(null);
    setCompareSource(null);
    setTool(nextTool);
    setError(null);
    setPreflight(null);
  }

  async function checkPrint() {
    if (!displayed) return;
    setBusy(true);
    setError(null);
    try {
      setPreflight(await runPreflight(displayed.id, targetWidthCm, targetDpi));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "印前检查失败");
    } finally {
      setBusy(false);
    }
  }

  const sourceName = String(source?.metadata?.original_filename ?? operationLabels[source?.operation ?? ""] ?? "未选择");

  return (
    <div className="pod-shell">
      <header className="pod-topbar">
        <div className="pod-brand">
          <span className="pod-brand-mark"><ImagePlus size={19} /></span>
          <div><strong>Sub2Image</strong><span>POD Studio</span></div>
        </div>
        <div className="pod-engine-state">
          <span className={capabilities?.ai_upscale_available ? "pod-dot ready" : "pod-dot"} />
          <span>{capabilities?.ai_upscale_available ? "Real-ESRGAN 已就绪" : "高清引擎不可用"}</span>
        </div>
      </header>

      <main className="pod-workspace">
        <nav className="pod-toolrail" aria-label="POD 图片工具">
          {workflowTools.map(({ id, short, label, icon: Icon }) => (
            <button
              className={tool === id ? "pod-tool active" : "pod-tool"}
              key={id}
              onClick={() => selectTool(id)}
              title={label}
              type="button"
            >
              <Icon size={20} strokeWidth={1.8} />
              <span>{short}</span>
            </button>
          ))}
        </nav>

        <section className="pod-stage">
          <div className="pod-stage-header">
            <div>
              <span className="pod-eyebrow">{selectedTool.label}</span>
              <h1>{tool === "extract-print" ? "从产品图还原印花" : selectedTool.label}</h1>
            </div>
            <div className="pod-stage-actions">
              <button className="pod-icon-button" onClick={() => fileInput.current?.click()} title="上传素材" type="button">
                <Upload size={18} />
              </button>
              {displayed && (
                <a className="pod-icon-button" href={displayed.download_url} title="下载当前结果">
                  <Download size={18} />
                </a>
              )}
            </div>
          </div>

          <div
            className={dragging ? "pod-canvas dragging" : "pod-canvas"}
            onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={(event) => { if (event.currentTarget === event.target) setDragging(false); }}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              void acceptFile(event.dataTransfer.files[0]);
            }}
          >
            {result ? (
              <CompareCanvas result={result} source={compareSource} />
            ) : source && tool === "extract-print" ? (
              <CropCanvas asset={source} crop={crop} onChange={setCrop} />
            ) : displayed ? (
              <img className="pod-preview-image" src={displayed.url} alt="当前素材" />
            ) : (
              <button className="pod-empty" onClick={() => fileInput.current?.click()} type="button">
                <Upload size={36} />
                <strong>选择或拖入产品图片</strong>
                <span>PNG · JPEG · WebP · 最大 20 MB</span>
              </button>
            )}
            {busy && (
              <div className="pod-processing" role="status">
                <LoaderCircle className="spin" size={29} />
                <strong>{tool === "restore" ? "正在重建图稿细节" : "正在处理任务"}</strong>
              </div>
            )}
          </div>

          {displayed && (
            <>
              <div className="pod-result-meta">
                <span>{operationLabels[displayed.operation] ?? displayed.operation}</span>
                <span>{displayed.width} × {displayed.height}</span>
                <span>{formatBytes(displayed.size_bytes)}</span>
                {displayed.metadata?.engine != null && <span>{String(displayed.metadata.engine)}</span>}
              </div>
              {displayed.warning && <div className="pod-result-warning"><AlertTriangle size={15} />{displayed.warning}</div>}

              <div className="pod-chain-actions" aria-label="继续处理">
                <span>继续处理</span>
                <button onClick={() => continueWith("extract-print")} type="button"><Crop size={15} />提取印花</button>
                <button onClick={() => continueWith("restore")} type="button"><ScanLine size={15} />高清修复</button>
                <button onClick={() => continueWith("remove-background")} type="button"><Eraser size={15} />主体抠图</button>
                <button onClick={() => continueWith("ai-reconstruct")} type="button"><WandSparkles size={15} />AI 重建</button>
              </div>
            </>
          )}

          {lineage.length > 1 && (
            <section className="pod-lineage" aria-label="处理路径">
              <div className="pod-section-title"><History size={15} /><strong>处理路径</strong></div>
              <div className="pod-lineage-list">
                {lineage.map((item, index) => (
                  <button key={item.id} onClick={() => useAsset(item)} type="button">
                    <span>{index + 1}</span>{operationLabels[item.operation] ?? item.operation}
                  </button>
                ))}
              </div>
            </section>
          )}

          {assets.length > 0 && (
            <section className="pod-library" aria-label="素材库">
              <div className="pod-section-title"><History size={15} /><strong>素材与结果</strong><span>{assets.length}</span></div>
              <div className="pod-library-list">
                {assets.map((asset) => (
                  <button className={displayed?.id === asset.id ? "selected" : ""} key={asset.id} onClick={() => useAsset(asset)} type="button">
                    <img src={asset.url} alt={operationLabels[asset.operation] ?? "素材"} />
                    <span>{operationLabels[asset.operation] ?? asset.operation}</span>
                  </button>
                ))}
              </div>
            </section>
          )}
        </section>

        <aside className="pod-controls">
          <div className="pod-controls-header">
            <div><span className="pod-eyebrow">任务参数</span><h2>{selectedTool.label}</h2></div>
            {needsSource && <span className="pod-source-name" title={sourceName}>{sourceName}</span>}
          </div>

          {needsSource && !source && (
            <button className="pod-upload-control" onClick={() => fileInput.current?.click()} type="button">
              <Upload size={17} />选择产品图片
            </button>
          )}

          {tool === "extract-print" && (
            <>
              <div className="pod-selection-readout">
                <Crop size={17} />
                <span>X {percent(crop.x)} · Y {percent(crop.y)} · W {percent(crop.width)} · H {percent(crop.height)}</span>
                <button onClick={() => setCrop({ x: 0, y: 0, width: 1, height: 1 })} type="button">全图</button>
              </div>
              <RangeField label="衣服底色容差" value={backgroundTolerance} onChange={setBackgroundTolerance} />
              <RangeField label="布纹抑制" value={textureReduction} onChange={setTextureReduction} />
              <RangeField label="阴影校正" value={shadowReduction} onChange={setShadowReduction} />
              <RangeField label="边缘净化" value={edgeCleanup} onChange={setEdgeCleanup} />
            </>
          )}

          {tool === "restore" && (
            <>
              <SegmentedControl
                label="图稿类型"
                options={["faithful", "illustration", "logo"]}
                value={restoreMode}
                onChange={(value) => setRestoreMode(value as RestoreMode)}
                labels={{ faithful: "照片/复杂图", illustration: "插画", logo: "文字 Logo" }}
              />
              <SegmentedControl
                label="输出倍率"
                options={["2", "4"]}
                value={String(scale)}
                onChange={(value) => setScale(value === "2" ? 2 : 4)}
                labels={{ "2": "2×", "4": "4×" }}
              />
              <RangeField label="压缩噪点清理" value={denoise} onChange={setDenoise} />
              <RangeField label="去模糊与边缘恢复" value={deblur} onChange={setDeblur} />
              <div className={restoreMode === "logo" ? "pod-engine-note structural" : "pod-engine-note"}>
                <ScanLine size={18} />
                <div>
                  <strong>{restoreMode === "logo" ? "色块与轮廓重建" : restoreMode === "illustration" ? "Real-ESRGAN Anime" : "Real-ESRGAN x4plus"}</strong>
                  <span>{restoreMode === "logo" ? "确定性处理" : "神经网络细节重建"}</span>
                </div>
              </div>
            </>
          )}

          {tool === "remove-background" && (
            <div className="pod-engine-note structural">
              <Eraser size={18} />
              <div><strong>{capabilities?.background_model ?? "U²-Net"}</strong><span>主体分割 · 透明 PNG</span></div>
            </div>
          )}

          {(tool === "generate" || tool === "ai-reconstruct") && (
            <>
              {tool === "ai-reconstruct" && (
                <div className="pod-risk-note"><AlertTriangle size={17} /><span>推测性重建：可能改变文字、Logo、人物与图形结构。</span></div>
              )}
              <div className="pod-field">
                <label htmlFor="prompt">{tool === "generate" ? "画面描述" : "重建要求"}</label>
                <textarea id="prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={5} maxLength={5000} />
              </div>
              <div className="pod-field">
                <label htmlFor="size">输出尺寸</label>
                <select id="size" value={size} onChange={(event) => setSize(event.target.value as ImageSize)}>
                  <option value="auto">自动</option>
                  <option value="1024x1024">1024 × 1024</option>
                  <option value="1024x1536">1024 × 1536</option>
                  <option value="1536x1024">1536 × 1024</option>
                </select>
              </div>
              <SegmentedControl
                label="质量"
                options={["low", "medium", "high"]}
                value={quality}
                onChange={(value) => setQuality(value as ImageQuality)}
                labels={{ low: "快速", medium: "标准", high: "精细" }}
              />
            </>
          )}

          {capabilities?.sub2api_configured === false && (tool === "generate" || tool === "ai-reconstruct") && (
            <div className="pod-config-warning">后端尚未配置 Sub2API 密钥</div>
          )}
          {error && <div className="pod-error" role="alert">{error}</div>}

          <button className="pod-primary" disabled={!canRun} onClick={() => void submit()} type="button">
            {busy ? <LoaderCircle className="spin" size={18} /> : <selectedTool.icon size={18} />}
            {busy ? "处理中" : tool === "restore" ? "开始高清修复" : tool === "extract-print" ? "提取印花" : "创建任务"}
          </button>

          {displayed && (
            <section className="pod-preflight">
              <div className="pod-section-title"><ShieldCheck size={16} /><strong>印前检查</strong></div>
              <div className="pod-preflight-inputs">
                <label>成品宽度<input type="number" min="1" max="200" value={targetWidthCm} onChange={(event) => setTargetWidthCm(Number(event.target.value))} /><span>cm</span></label>
                <label>目标精度<select value={targetDpi} onChange={(event) => setTargetDpi(Number(event.target.value))}><option value="150">150 DPI</option><option value="200">200 DPI</option><option value="300">300 DPI</option><option value="600">600 DPI</option></select></label>
              </div>
              <button className="pod-secondary" onClick={() => void checkPrint()} disabled={busy} type="button">检查当前文件</button>
              {preflight && <PreflightView report={preflight} />}
            </section>
          )}
        </aside>
      </main>
      <input ref={fileInput} type="file" accept="image/png,image/jpeg,image/webp" hidden onChange={(event) => void acceptFile(event.target.files?.[0])} />
    </div>
  );
}

function CropCanvas({ asset, crop, onChange }: { asset: ImageResult; crop: CropRect; onChange: (crop: CropRect) => void }) {
  const frame = useRef<HTMLDivElement>(null);
  const start = useRef<{ x: number; y: number } | null>(null);
  const [frameSize, setFrameSize] = useState({ width: 1, height: 1 });

  useEffect(() => {
    const element = frame.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setFrameSize({ width: entry.contentRect.width, height: entry.contentRect.height }));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const fitted = fitRect(frameSize.width, frameSize.height, asset.width, asset.height);
  function point(event: React.PointerEvent): { x: number; y: number } {
    const bounds = frame.current!.getBoundingClientRect();
    return {
      x: clamp((event.clientX - bounds.left - fitted.left) / fitted.width),
      y: clamp((event.clientY - bounds.top - fitted.top) / fitted.height),
    };
  }

  return (
    <div
      className="pod-crop-frame"
      ref={frame}
      onPointerDown={(event) => {
        const next = point(event);
        start.current = next;
        event.currentTarget.setPointerCapture(event.pointerId);
        onChange({ x: next.x, y: next.y, width: 0.02, height: 0.02 });
      }}
      onPointerMove={(event) => {
        if (!start.current) return;
        const next = point(event);
        const x = Math.min(start.current.x, next.x);
        const y = Math.min(start.current.y, next.y);
        onChange({ x, y, width: Math.max(0.02, Math.abs(next.x - start.current.x)), height: Math.max(0.02, Math.abs(next.y - start.current.y)) });
      }}
      onPointerUp={() => { start.current = null; }}
    >
      <img src={asset.url} alt="选择印花区域" draggable={false} />
      <div className="pod-crop-shade" />
      <div
        className="pod-crop-box"
        style={{
          left: fitted.left + crop.x * fitted.width,
          top: fitted.top + crop.y * fitted.height,
          width: crop.width * fitted.width,
          height: crop.height * fitted.height,
        }}
      ><i /><i /><i /><i /></div>
    </div>
  );
}

function CompareCanvas({ result, source }: { result: ImageResult; source: ImageResult | null }) {
  const [compare, setCompare] = useState(50);
  return (
    <div className="pod-compare">
      <img src={result.url} alt="处理结果" />
      {source && (
        <>
          <div className="pod-before" style={{ clipPath: `inset(0 ${100 - compare}% 0 0)` }}><img src={source.url} alt="处理前" /></div>
          <div className="pod-compare-line" style={{ left: `${compare}%` }} />
          <input type="range" min="0" max="100" value={compare} onChange={(event) => setCompare(Number(event.target.value))} aria-label="调整前后对比" />
          <span className="pod-before-label">处理前</span><span className="pod-after-label">结果</span>
        </>
      )}
    </div>
  );
}

function RangeField({ label, value, onChange }: { label: string; value: number; onChange: (value: number) => void }) {
  return <div className="pod-range"><div><label>{label}</label><span>{value}</span></div><input type="range" min="0" max="100" value={value} onChange={(event) => onChange(Number(event.target.value))} /></div>;
}

function SegmentedControl({ label, options, value, onChange, labels }: { label: string; options: string[]; value: string; onChange: (value: string) => void; labels: Record<string, string> }) {
  return <div className="pod-field"><label>{label}</label><div className="pod-segmented">{options.map((option) => <button className={option === value ? "selected" : ""} key={option} onClick={() => onChange(option)} type="button">{labels[option]}</button>)}</div></div>;
}

function PreflightView({ report }: { report: PreflightReport }) {
  const ready = report.status === "ready";
  return (
    <div className={`pod-preflight-result ${report.status}`}>
      <div><span className="pod-preflight-icon">{ready ? <Check size={16} /> : <AlertTriangle size={16} />}</span><strong>{ready ? "尺寸达标" : report.status === "review" ? "建议复核" : "分辨率不足"}</strong></div>
      <dl><div><dt>有效精度</dt><dd>{report.effective_dpi} DPI</dd></div><div><dt>所需宽度</dt><dd>{report.required_width_pixels} px</dd></div><div><dt>{report.target_dpi} DPI 最大宽度</dt><dd>{report.max_width_cm_at_target_dpi} cm</dd></div><div><dt>透明背景</dt><dd>{report.has_alpha ? "有效" : "无"}</dd></div></dl>
      {report.warnings.map((warning) => <p key={warning}>{warning}</p>)}
    </div>
  );
}

function fitRect(containerWidth: number, containerHeight: number, imageWidth: number, imageHeight: number) {
  const scale = Math.min(containerWidth / imageWidth, containerHeight / imageHeight);
  const width = imageWidth * scale;
  const height = imageHeight * scale;
  return { width, height, left: (containerWidth - width) / 2, top: (containerHeight - height) / 2 };
}

function clamp(value: number): number { return Math.min(1, Math.max(0, value)); }
function percent(value: number): string { return `${Math.round(value * 100)}%`; }
function formatBytes(bytes: number): string { return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`; }

export default PodApp;
