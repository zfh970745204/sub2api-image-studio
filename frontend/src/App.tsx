import {
  ArrowUpRight,
  Download,
  Eraser,
  ImagePlus,
  LoaderCircle,
  Maximize2,
  RefreshCw,
  Sparkles,
  Upload,
  WandSparkles,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { getCapabilities, runTool } from "./api";
import type {
  Capabilities,
  ImageQuality,
  ImageResult,
  ImageSize,
  OutputFormat,
  ToolId,
} from "./types";

const tools = [
  { id: "generate" as const, label: "生成", icon: Sparkles },
  { id: "edit" as const, label: "编辑", icon: WandSparkles },
  { id: "remove-background" as const, label: "抠图", icon: Eraser },
  { id: "upscale" as const, label: "高清", icon: Maximize2 },
];

const toolCopy: Record<ToolId, { title: string; action: string }> = {
  generate: { title: "生成图片", action: "开始生成" },
  edit: { title: "编辑图片", action: "应用编辑" },
  "remove-background": { title: "去除背景", action: "开始抠图" },
  upscale: { title: "原图高清", action: "开始放大" },
};

const promptPlaceholders: Record<"generate" | "edit", string> = {
  generate: "描述画面、主体、光线与构图…",
  edit: "例如：把背景换成干净的摄影棚白色背景…",
};

function App() {
  const [tool, setTool] = useState<ToolId>("generate");
  const [file, setFile] = useState<File | null>(null);
  const [sourceUrl, setSourceUrl] = useState<string | null>(null);
  const [prompt, setPrompt] = useState("");
  const [size, setSize] = useState<ImageSize>("1024x1024");
  const [quality, setQuality] = useState<ImageQuality>("medium");
  const [outputFormat, setOutputFormat] = useState<OutputFormat>("png");
  const [scale, setScale] = useState<2 | 4>(2);
  const [sharpen, setSharpen] = useState(true);
  const [compare, setCompare] = useState(50);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ImageResult | null>(null);
  const [history, setHistory] = useState<ImageResult[]>([]);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const requiresFile = tool !== "generate";
  const requiresPrompt = tool === "generate" || tool === "edit";
  const canSubmit =
    !busy &&
    (!requiresFile || Boolean(file)) &&
    (!requiresPrompt || Boolean(prompt.trim())) &&
    (!(tool === "generate" || tool === "edit") || capabilities?.sub2api_configured !== false);

  useEffect(() => {
    getCapabilities().then(setCapabilities).catch(() => setCapabilities(null));
  }, []);

  useEffect(() => {
    return () => {
      if (sourceUrl) URL.revokeObjectURL(sourceUrl);
    };
  }, [sourceUrl]);

  const selectedTool = useMemo(() => tools.find((item) => item.id === tool)!, [tool]);

  function selectTool(nextTool: ToolId) {
    setTool(nextTool);
    setResult(null);
    setError(null);
    setCompare(50);
  }

  function acceptFile(nextFile?: File) {
    if (!nextFile) return;
    if (!nextFile.type.startsWith("image/")) {
      setError("请选择 PNG、JPEG 或 WebP 图片");
      return;
    }
    setFile(nextFile);
    setSourceUrl(URL.createObjectURL(nextFile));
    setResult(null);
    setError(null);
  }

  function clearFile() {
    setFile(null);
    setSourceUrl(null);
    setResult(null);
    if (fileInput.current) fileInput.current.value = "";
  }

  async function submit() {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const next = await runTool({
        tool,
        file,
        prompt: prompt.trim(),
        size,
        quality,
        outputFormat,
        scale,
        sharpen,
      });
      setResult(next);
      setHistory((items) => [next, ...items.filter((item) => item.id !== next.id)].slice(0, 6));
      setCompare(50);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "处理失败，请重试");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-block">
          <span className="brand-mark" aria-hidden="true"><ImagePlus size={19} /></span>
          <div>
            <strong>Sub2Image</strong>
            <span>Studio</span>
          </div>
        </div>
        <div
          className="service-state"
          title={capabilities?.sub2api_configured ? capabilities.sub2api_model : "本地模式"}
        >
          <span className={capabilities?.sub2api_configured ? "status-dot online" : "status-dot"} />
          <span>{capabilities?.sub2api_configured ? capabilities.sub2api_model : "本地模式"}</span>
        </div>
      </header>

      <main className="workspace">
        <nav className="toolrail" aria-label="图片工具">
          {tools.map(({ id, label, icon: Icon }) => (
            <button
              className={tool === id ? "tool-button active" : "tool-button"}
              key={id}
              onClick={() => selectTool(id)}
              title={label}
              type="button"
            >
              <Icon size={21} strokeWidth={1.8} />
              <span>{label}</span>
            </button>
          ))}
        </nav>

        <section className="stage" aria-label="图片预览">
          <div className="stage-heading">
            <div>
              <span className="eyebrow">{selectedTool.label}</span>
              <h1>{toolCopy[tool].title}</h1>
            </div>
            {result && (
              <a className="download-icon" href={result.download_url} title="下载结果">
                <Download size={20} />
              </a>
            )}
          </div>

          <div
            className={dragging ? "canvas dragging" : "canvas"}
            onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={(event) => {
              if (event.currentTarget === event.target) setDragging(false);
            }}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              acceptFile(event.dataTransfer.files[0]);
            }}
          >
            {result ? (
              <ResultCanvas
                result={result}
                sourceUrl={sourceUrl}
                compare={compare}
                onCompare={setCompare}
              />
            ) : sourceUrl ? (
              <div className="single-preview">
                <img src={sourceUrl} alt="待处理原图" />
                <button className="clear-image" onClick={clearFile} title="移除图片" type="button">
                  <X size={18} />
                </button>
              </div>
            ) : (
              <button
                className="empty-canvas"
                onClick={() => requiresFile && fileInput.current?.click()}
                type="button"
              >
                {tool === "generate" ? <Sparkles size={38} /> : <Upload size={38} />}
                <strong>{tool === "generate" ? "等待创作" : "选择或拖入图片"}</strong>
                <span>{tool === "generate" ? "在右侧输入画面描述" : "PNG · JPEG · WebP · 最大 20 MB"}</span>
              </button>
            )}

            {busy && (
              <div className="processing" role="status">
                <LoaderCircle className="spin" size={30} />
                <strong>{tool === "remove-background" ? "正在分离主体" : tool === "upscale" ? "正在放大图像" : "正在等待模型"}</strong>
              </div>
            )}
          </div>

          {result && (
            <div className="result-meta">
              <span>{result.width} × {result.height}</span>
              <span>{formatBytes(result.size_bytes)}</span>
              <span>{result.mime_type.replace("image/", "").toUpperCase()}</span>
              {result.warning && <span className="result-warning">{result.warning}</span>}
            </div>
          )}

          {history.length > 0 && (
            <section className="history-strip" aria-label="最近结果">
              <div className="history-title">
                <strong>最近结果</strong>
                <span>{history.length}</span>
              </div>
              <div className="history-list">
                {history.map((item) => (
                  <button key={item.id} onClick={() => setResult(item)} type="button" title="查看结果">
                    <img src={item.url} alt="处理结果缩略图" />
                    <ArrowUpRight size={14} />
                  </button>
                ))}
              </div>
            </section>
          )}
        </section>

        <aside className="controls">
          <div className="controls-header">
            <div>
              <span className="eyebrow">参数</span>
              <h2>{toolCopy[tool].title}</h2>
            </div>
            <button className="icon-button" onClick={() => { setResult(null); setError(null); }} title="重置结果" type="button">
              <RefreshCw size={17} />
            </button>
          </div>

          {requiresFile && (
            <div className="field-block">
              <label>原始图片</label>
              <button className="file-picker" onClick={() => fileInput.current?.click()} type="button">
                <Upload size={17} />
                <span>{file ? file.name : "选择图片"}</span>
              </button>
              <input
                ref={fileInput}
                type="file"
                accept="image/png,image/jpeg,image/webp"
                hidden
                onChange={(event) => acceptFile(event.target.files?.[0])}
              />
            </div>
          )}

          {requiresPrompt && (
            <div className="field-block">
              <label htmlFor="prompt">画面描述</label>
              <textarea
                id="prompt"
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                placeholder={promptPlaceholders[tool as "generate" | "edit"]}
                rows={6}
                maxLength={5000}
              />
              <span className="char-count">{prompt.length} / 5000</span>
            </div>
          )}

          {(tool === "generate" || tool === "edit") && (
            <>
              <div className="field-block">
                <label htmlFor="size">画布尺寸</label>
                <select id="size" value={size} onChange={(event) => setSize(event.target.value as ImageSize)}>
                  <option value="1024x1024">方形 · 1024 × 1024</option>
                  <option value="1024x1536">竖版 · 1024 × 1536</option>
                  <option value="1536x1024">横版 · 1536 × 1024</option>
                  <option value="auto">自动</option>
                </select>
              </div>
              <SegmentedControl
                label="生成质量"
                options={["low", "medium", "high"]}
                value={quality}
                onChange={(value) => setQuality(value as ImageQuality)}
                labels={{ low: "快速", medium: "标准", high: "精细" }}
              />
              <div className="field-block">
                <label htmlFor="format">输出格式</label>
                <select id="format" value={outputFormat} onChange={(event) => setOutputFormat(event.target.value as OutputFormat)}>
                  <option value="png">PNG</option>
                  <option value="webp">WebP</option>
                  <option value="jpeg">JPEG</option>
                </select>
              </div>
            </>
          )}

          {tool === "upscale" && (
            <>
              <SegmentedControl
                label="放大倍数"
                options={["2", "4"]}
                value={String(scale)}
                onChange={(value) => setScale(value === "4" ? 4 : 2)}
                labels={{ "2": "2×", "4": "4×" }}
              />
              <label className="toggle-row">
                <span>
                  <strong>边缘锐化</strong>
                  <small>适度恢复缩放后的边缘清晰度</small>
                </span>
                <input type="checkbox" checked={sharpen} onChange={(event) => setSharpen(event.target.checked)} />
              </label>
            </>
          )}

          {tool === "remove-background" && (
            <div className="engine-note">
              <Eraser size={18} />
              <div>
                <strong>{capabilities?.background_model ?? "u2net"}</strong>
                <span>本地处理 · 透明 PNG</span>
              </div>
            </div>
          )}

          {capabilities?.sub2api_configured === false && (tool === "generate" || tool === "edit") && (
            <div className="config-warning">后端尚未配置 Sub2API 密钥</div>
          )}
          {error && <div className="error-message" role="alert">{error}</div>}

          <button className="primary-action" disabled={!canSubmit} onClick={submit} type="button">
            {busy ? <LoaderCircle className="spin" size={19} /> : <selectedTool.icon size={19} />}
            <span>{busy ? "处理中" : toolCopy[tool].action}</span>
          </button>
        </aside>
      </main>
    </div>
  );
}

function ResultCanvas({
  result,
  sourceUrl,
  compare,
  onCompare,
}: {
  result: ImageResult;
  sourceUrl: string | null;
  compare: number;
  onCompare: (value: number) => void;
}) {
  return (
    <div className="result-canvas">
      <img className="result-image" src={result.url} alt="处理结果" />
      {sourceUrl && (
        <>
          <div
            className="source-clip"
            style={{ clipPath: `inset(0 ${100 - compare}% 0 0)` }}
          >
            <img src={sourceUrl} alt="处理前原图" />
          </div>
          <div className="compare-line" style={{ left: `${compare}%` }} aria-hidden="true">
            <span />
          </div>
          <input
            className="compare-range"
            type="range"
            min="0"
            max="100"
            value={compare}
            onChange={(event) => onCompare(Number(event.target.value))}
            aria-label="调整前后对比"
          />
          <span className="before-label">原图</span>
          <span className="after-label">结果</span>
        </>
      )}
    </div>
  );
}

function SegmentedControl({
  label,
  options,
  value,
  onChange,
  labels,
}: {
  label: string;
  options: string[];
  value: string;
  onChange: (value: string) => void;
  labels: Record<string, string>;
}) {
  return (
    <div className="field-block">
      <label>{label}</label>
      <div className="segmented">
        {options.map((option) => (
          <button
            className={option === value ? "selected" : ""}
            key={option}
            onClick={() => onChange(option)}
            type="button"
          >
            {labels[option]}
          </button>
        ))}
      </div>
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default App;
