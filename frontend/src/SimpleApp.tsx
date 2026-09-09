import {
  ArrowLeft,
  Brush,
  Check,
  Contrast,
  Download,
  FileImage,
  History,
  ImagePlus,
  LoaderCircle,
  PaintBucket,
  Palette,
  PenTool,
  Plus,
  RotateCcw,
  Scaling,
  Scissors,
  Shuffle,
  Sparkles,
  Type,
  Undo2,
  Upload,
  Trash2,
  WandSparkles,
} from "lucide-react";
import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import {
  applyColorEffect,
  cutoutAsset,
  generateArtwork,
  getAssets,
  getCapabilities,
  getLineage,
  transformAsset,
  uploadAsset,
  upscaleAsset,
  vectorizeAsset,
} from "./api";
import type { AiTransformMode, ColorEffectMode } from "./api";
import type { Capabilities, ImageResult } from "./types";

type ToolKey = "enhance" | "cutout" | "upscale" | "repair" | "text" | "color" | "variant" | "vector";
type BusyKey = "upload" | "generate" | ToolKey;
type CutoutPreviewMode = "transparent" | "white" | "neutral" | "dark" | "custom" | "image";

const tools: Array<{ id: ToolKey; label: string; icon: typeof Sparkles }> = [
  { id: "enhance", label: "高清重绘", icon: Sparkles },
  { id: "cutout", label: "智能抠图", icon: Scissors },
  { id: "upscale", label: "尺寸放大", icon: Scaling },
  { id: "repair", label: "局部修复", icon: Brush },
  { id: "text", label: "文字修正", icon: Type },
  { id: "color", label: "颜色处理", icon: Palette },
  { id: "variant", label: "生成变体", icon: Shuffle },
  { id: "vector", label: "矢量化", icon: PenTool },
];

const enhanceModes: Array<{ id: AiTransformMode; label: string }> = [
  { id: "faithful-redraw", label: "忠实重绘" },
  { id: "photo-enhance", label: "照片高清" },
  { id: "illustration-enhance", label: "插画高清" },
  { id: "logo-cleanup", label: "Logo 清理" },
  { id: "line-art", label: "线稿增强" },
];

const artworkStyles = [
  { id: "graphic", label: "图形插画" },
  { id: "vintage", label: "复古印花" },
  { id: "line", label: "黑白线稿" },
  { id: "cute", label: "卡通风格" },
];

const variantStyles = [
  { id: "faithful", label: "相近版本", prompt: "Create a faithful alternative with only subtle visual variation." },
  { id: "simple", label: "更简洁", prompt: "Simplify secondary detail while preserving the main subject and message." },
  { id: "detail", label: "更细致", prompt: "Add refined intentional detail without changing the main composition or text." },
];

const colorSwatches = ["#111111", "#ffffff", "#d82c3f", "#f2b827", "#2d8c65", "#2467c9", "#7b42b3", "#ef6f35"];

const cutoutPreviewOptions: Array<{
  id: Exclude<CutoutPreviewMode, "custom" | "image">;
  label: string;
}> = [
  { id: "transparent", label: "透明" },
  { id: "white", label: "白色" },
  { id: "neutral", label: "浅灰" },
  { id: "dark", label: "深色" },
];

function SimpleApp() {
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [assets, setAssets] = useState<ImageResult[]>([]);
  const [lineage, setLineage] = useState<ImageResult[]>([]);
  const [current, setCurrent] = useState<ImageResult | null>(null);
  const [previous, setPrevious] = useState<ImageResult | null>(null);
  const [activeTool, setActiveTool] = useState<ToolKey>("enhance");
  const [busy, setBusy] = useState<BusyKey | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showCompare, setShowCompare] = useState(false);
  const [generationPrompt, setGenerationPrompt] = useState("");
  const [generationStyle, setGenerationStyle] = useState("graphic");
  const [enhanceMode, setEnhanceMode] = useState<AiTransformMode>("faithful-redraw");
  const [repairPrompt, setRepairPrompt] = useState("");
  const [correctText, setCorrectText] = useState("");
  const [variantStyle, setVariantStyle] = useState("faithful");
  const [variantPrompt, setVariantPrompt] = useState("");
  const [selectedColor, setSelectedColor] = useState("#111111");
  const [upscaleScale, setUpscaleScale] = useState<2 | 4>(2);
  const [cutoutPreviewMode, setCutoutPreviewMode] = useState<CutoutPreviewMode>("transparent");
  const [cutoutPreviewColor, setCutoutPreviewColor] = useState("#f0d7cf");
  const [cutoutPreviewImage, setCutoutPreviewImage] = useState<string | null>(null);
  const [cutoutPreviewImageName, setCutoutPreviewImageName] = useState("");
  const [maskSelected, setMaskSelected] = useState(false);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const cutoutPreviewImageRef = useRef<string | null>(null);
  const maskRef = useRef<MaskCanvasHandle>(null);

  useEffect(() => {
    Promise.all([getCapabilities(), getAssets()])
      .then(([nextCapabilities, nextAssets]) => {
        setCapabilities(nextCapabilities);
        setAssets(nextAssets);
      })
      .catch((reason) => setError(errorMessage(reason, "服务连接失败")));
  }, []);

  useEffect(() => () => {
    if (cutoutPreviewImageRef.current) {
      URL.revokeObjectURL(cutoutPreviewImageRef.current);
    }
  }, []);

  useEffect(() => {
    if (!current) {
      setLineage([]);
      return;
    }
    const rootId = current.root_id || current.id;
    const knownVersions = assets.filter((asset) => (asset.root_id || asset.id) === rootId);
    setLineage(sortVersions(knownVersions.length ? knownVersions : [current]));
    let active = true;
    getLineage(current.id)
      .then((items) => {
        if (active) setLineage(sortVersions(uniqueAssets([...knownVersions, ...items])));
      })
      .catch(() => {
        if (active) setLineage(sortVersions(knownVersions.length ? knownVersions : [current]));
      });
    return () => {
      active = false;
    };
  }, [assets, current]);

  async function acceptFile(file?: File) {
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      setError("请选择 PNG、JPEG 或 WebP 图片");
      return;
    }
    setBusy("upload");
    clearMessages();
    try {
      const uploaded = await uploadAsset(file);
      openAsset(uploaded, null);
      addAsset(uploaded);
    } catch (reason) {
      setError(errorMessage(reason, "上传失败"));
    } finally {
      setBusy(null);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function createArtwork() {
    if (!generationPrompt.trim()) {
      setError("请输入图案内容");
      return;
    }
    setBusy("generate");
    clearMessages();
    try {
      const generated = await generateArtwork(generationPrompt.trim(), generationStyle);
      openAsset(generated, null);
      addAsset(generated);
      setActiveTool("cutout");
      setNotice("图案已生成，可继续抠图或编辑");
    } catch (reason) {
      setError(errorMessage(reason, "生成图案失败"));
    } finally {
      setBusy(null);
    }
  }

  async function execute(tool: ToolKey, task: () => Promise<ImageResult>, success: string) {
    if (!current) return;
    setBusy(tool);
    clearMessages();
    try {
      const result = await task();
      setPrevious(current);
      setCurrent(result);
      setShowCompare(true);
      addAsset(result);
      setMaskSelected(false);
      setNotice(success);
    } catch (reason) {
      setError(errorMessage(reason, "图片处理失败"));
    } finally {
      setBusy(null);
    }
  }

  async function runMasked(mode: "local-repair" | "text-fix", instruction: string) {
    if (!current || !instruction.trim()) {
      setError(mode === "text-fix" ? "请输入正确文字" : "请输入修复内容");
      return;
    }
    let mask: File;
    try {
      mask = await maskRef.current!.toMaskFile();
    } catch (reason) {
      setError(errorMessage(reason, "请先涂抹修改区域"));
      return;
    }
    await execute(
      mode === "text-fix" ? "text" : "repair",
      () => transformAsset(current.id, mode, instruction.trim(), mask),
      mode === "text-fix" ? "文字修正版已生成" : "局部修复已完成",
    );
  }

  function addAsset(asset: ImageResult) {
    setAssets((items) => [asset, ...items.filter((item) => item.id !== asset.id)].slice(0, 40));
  }

  function openAsset(asset: ImageResult, compareWith?: ImageResult | null) {
    const parent = compareWith === undefined
      ? assets.find((item) => item.id === asset.parent_id) ?? null
      : compareWith;
    setCurrent(asset);
    setPrevious(parent);
    setShowCompare(Boolean(parent));
    setMaskSelected(false);
    setActiveTool("enhance");
    clearMessages();
  }

  function clearMessages() {
    setError(null);
    setNotice(null);
  }

  function chooseCutoutPreviewImage(file?: File) {
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      setError("请选择一张图片作为预览背景");
      return;
    }
    if (cutoutPreviewImageRef.current) {
      URL.revokeObjectURL(cutoutPreviewImageRef.current);
    }
    const imageUrl = URL.createObjectURL(file);
    cutoutPreviewImageRef.current = imageUrl;
    setCutoutPreviewImage(imageUrl);
    setCutoutPreviewImageName(file.name);
    setCutoutPreviewMode("image");
    clearMessages();
  }

  function removeCutoutPreviewImage() {
    if (cutoutPreviewImageRef.current) {
      URL.revokeObjectURL(cutoutPreviewImageRef.current);
      cutoutPreviewImageRef.current = null;
    }
    setCutoutPreviewImage(null);
    setCutoutPreviewImageName("");
    setCutoutPreviewMode("transparent");
  }

  function newTask() {
    setCurrent(null);
    setPrevious(null);
    setLineage([]);
    setShowCompare(false);
    setMaskSelected(false);
    clearMessages();
  }

  const svgDownload = typeof current?.metadata?.svg_download_url === "string"
    ? current.metadata.svg_download_url
    : null;
  const currentVersion = current
    ? lineage.findIndex((asset) => asset.id === current.id) + 1
    : 0;
  const cutoutPreviewClass = activeTool === "cutout"
    ? ` cutout-preview preview-${cutoutPreviewMode}`
    : "";
  const cutoutPreviewStyle = activeTool === "cutout" && cutoutPreviewMode === "custom"
    ? { backgroundColor: cutoutPreviewColor }
    : activeTool === "cutout" && cutoutPreviewMode === "image" && cutoutPreviewImage
      ? { backgroundImage: `url("${cutoutPreviewImage}")` }
      : undefined;

  return (
    <div className="studio-shell">
      <header className="studio-topbar">
        <button className="studio-brand" onClick={newTask} type="button">
          <span><ImagePlus size={19} /></span>
          <strong>Sub2Image</strong>
          <small>AI 图片工作台</small>
        </button>
        <div className="studio-top-actions">
          <div className="studio-status" title="图片服务状态">
            <i className={capabilities?.sub2api_configured ? "online" : ""} />
            <span>{capabilities?.sub2api_configured ? "服务可用" : "维护中"}</span>
          </div>
          <button onClick={() => fileInput.current?.click()} title="上传新图片" type="button"><Plus size={18} /></button>
        </div>
      </header>

      {!current ? (
        <Home
          assets={assets}
          busy={busy}
          dragging={dragging}
          error={error}
          generationPrompt={generationPrompt}
          generationStyle={generationStyle}
          onChoose={openAsset}
          onCreate={() => void createArtwork()}
          onDrop={(file) => void acceptFile(file)}
          onPrompt={setGenerationPrompt}
          onStyle={setGenerationStyle}
          onUpload={() => fileInput.current?.click()}
          setDragging={setDragging}
        />
      ) : (
        <main className="studio-layout">
          <nav className="studio-toolrail" aria-label="图片工具">
            {tools.map((tool) => {
              const Icon = tool.icon;
              return (
                <button
                  aria-current={activeTool === tool.id ? "page" : undefined}
                  aria-label={tool.label}
                  className={activeTool === tool.id ? "active" : ""}
                  key={tool.id}
                  onClick={() => {
                    setActiveTool(tool.id);
                    setShowCompare(false);
                    setMaskSelected(false);
                    clearMessages();
                  }}
                  type="button"
                >
                  <Icon size={18} /><span>{tool.label}</span>
                </button>
              );
            })}
          </nav>

          <section className="studio-workspace">
            <div className="studio-workspace-head">
              <button onClick={newTask} type="button"><ArrowLeft size={17} />图片库</button>
              <div>
                {previous && activeTool !== "repair" && activeTool !== "text" && (
                  <button
                    className={showCompare ? "active" : ""}
                    onClick={() => setShowCompare((value) => !value)}
                    type="button"
                  ><Contrast size={16} />对比</button>
                )}
                <a href={current.download_url} title="下载当前图片"><Download size={17} /></a>
              </div>
            </div>

            <div className={`studio-stage${cutoutPreviewClass}`} style={cutoutPreviewStyle}>
              {activeTool === "repair" || activeTool === "text" ? (
                <MaskCanvas
                  asset={current}
                  onSelectionChange={setMaskSelected}
                  ref={maskRef}
                />
              ) : showCompare && previous ? (
                <Compare key={`${previous.id}-${current.id}`} result={current} source={previous} />
              ) : (
                <img src={current.url} alt="当前图片" />
              )}
              {busy && busy !== "upload" && busy !== "generate" && (
                <BusyOverlay label={busyLabel(busy)} />
              )}
            </div>

            <div className="studio-image-meta">
              <span>{operationLabel(current)}</span>
              <span>{current.width} × {current.height}</span>
              <span>{formatBytes(current.size_bytes)}</span>
              {lineage.length > 1 && currentVersion > 0 && (
                <span>V{currentVersion} / {lineage.length}</span>
              )}
            </div>
          </section>

          <aside className="studio-controls">
            <ToolPanel
              activeTool={activeTool}
              busy={busy}
              correctText={correctText}
              cutoutPreviewColor={cutoutPreviewColor}
              cutoutPreviewImage={cutoutPreviewImage}
              cutoutPreviewImageName={cutoutPreviewImageName}
              cutoutPreviewMode={cutoutPreviewMode}
              current={current}
              enhanceMode={enhanceMode}
              maskSelected={maskSelected}
              repairPrompt={repairPrompt}
              selectedColor={selectedColor}
              upscaleScale={upscaleScale}
              setCorrectText={setCorrectText}
              setCutoutPreviewColor={setCutoutPreviewColor}
              setCutoutPreviewMode={setCutoutPreviewMode}
              setEnhanceMode={setEnhanceMode}
              setRepairPrompt={setRepairPrompt}
              setSelectedColor={setSelectedColor}
              setUpscaleScale={setUpscaleScale}
              setVariantPrompt={setVariantPrompt}
              setVariantStyle={setVariantStyle}
              svgDownload={svgDownload}
              variantPrompt={variantPrompt}
              variantStyle={variantStyle}
              onClearMask={() => maskRef.current?.clear()}
              onUndoMask={() => maskRef.current?.undo()}
              onEnhance={() => void execute(
                "enhance",
                () => transformAsset(current.id, enhanceMode),
                "高清版本已生成",
              )}
              onCutout={() => void execute(
                "cutout",
                () => cutoutAsset(current.id),
                "透明背景已生成",
              )}
              onCutoutPreviewImage={chooseCutoutPreviewImage}
              onRemoveCutoutPreviewImage={removeCutoutPreviewImage}
              onUpscale={() => void execute(
                "upscale",
                () => upscaleAsset(current.id, upscaleScale),
                `${upscaleScale}× 放大版本已生成`,
              )}
              onRepair={() => void runMasked("local-repair", repairPrompt)}
              onTextFix={() => void runMasked("text-fix", `Replace the selected wording with exactly: ${correctText.trim()}`)}
              onColor={(mode) => void execute(
                "color",
                () => applyColorEffect(current.id, mode, selectedColor),
                "颜色版本已生成",
              )}
              onVariant={() => {
                const preset = variantStyles.find((item) => item.id === variantStyle)!;
                const instruction = `${preset.prompt}${variantPrompt.trim() ? ` ${variantPrompt.trim()}` : ""}`;
                void execute("variant", () => transformAsset(current.id, "variant", instruction), "新变体已生成");
              }}
              onVector={() => void execute(
                "vector",
                () => vectorizeAsset(current.id),
                "SVG 矢量文件已生成",
              )}
            />

            {(error || notice) && (
              <div className={error ? "studio-message error" : "studio-message success"}>
                {error ?? notice}
              </div>
            )}

            <section className="studio-history">
              <div><History size={15} /><strong>当前版本链</strong><span>{lineage.length}</span></div>
              <div className="studio-history-list">
                {lineage.map((asset, index) => (
                  <button
                    className={asset.id === current.id ? "active" : ""}
                    key={asset.id}
                    onClick={() => openAsset(asset)}
                    title={operationLabel(asset)}
                    type="button"
                  >
                    <span className="studio-history-thumb"><img src={asset.url} alt="历史图片" /></span>
                    <span className="studio-history-copy">
                      <strong>{operationLabel(asset)}</strong>
                      <small>V{index + 1} · {asset.width} × {asset.height}</small>
                    </span>
                    {asset.id === current.id && <Check size={15} />}
                  </button>
                ))}
              </div>
            </section>
          </aside>
        </main>
      )}

      {busy === "upload" || busy === "generate" ? <BusyOverlay label={busyLabel(busy)} fixed /> : null}
      <input
        accept="image/png,image/jpeg,image/webp"
        hidden
        onChange={(event) => void acceptFile(event.target.files?.[0])}
        ref={fileInput}
        type="file"
      />
    </div>
  );
}

function Home({
  assets,
  busy,
  dragging,
  error,
  generationPrompt,
  generationStyle,
  onChoose,
  onCreate,
  onDrop,
  onPrompt,
  onStyle,
  onUpload,
  setDragging,
}: {
  assets: ImageResult[];
  busy: BusyKey | null;
  dragging: boolean;
  error: string | null;
  generationPrompt: string;
  generationStyle: string;
  onChoose: (asset: ImageResult) => void;
  onCreate: () => void;
  onDrop: (file?: File) => void;
  onPrompt: (value: string) => void;
  onStyle: (value: string) => void;
  onUpload: () => void;
  setDragging: (value: boolean) => void;
}) {
  const tasks = assets.filter((asset, index, items) => {
    const rootId = asset.root_id || asset.id;
    return items.findIndex((item) => (item.root_id || item.id) === rootId) === index;
  });

  return (
    <main className="studio-home">
      <header>
        <span>图片任务</span>
        <h1>开始处理图片</h1>
      </header>
      <div className="studio-home-entries">
        <section
          className={dragging ? "studio-upload-entry dragging" : "studio-upload-entry"}
          onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
          onDragLeave={(event) => { if (event.currentTarget === event.target) setDragging(false); }}
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            onDrop(event.dataTransfer.files[0]);
          }}
        >
          <button disabled={Boolean(busy)} onClick={onUpload} type="button">
            <span><Upload size={25} /></span>
            <strong>上传图片</strong>
            <small>PNG · JPEG · WebP</small>
          </button>
        </section>

        <section className="studio-generate-entry">
          <div><WandSparkles size={18} /><strong>AI 生成图案</strong></div>
          <textarea
            maxLength={1000}
            onChange={(event) => onPrompt(event.target.value)}
            placeholder="输入图案内容，例如：戴墨镜的骷髅骑摩托车"
            value={generationPrompt}
          />
          <div className="studio-segments compact">
            {artworkStyles.map((style) => (
              <button className={style.id === generationStyle ? "active" : ""} key={style.id} onClick={() => onStyle(style.id)} type="button">{style.label}</button>
            ))}
          </div>
          <button className="studio-primary" disabled={Boolean(busy)} onClick={onCreate} type="button"><Sparkles size={17} />生成图案</button>
        </section>
      </div>
      {error && <div className="studio-message error">{error}</div>}
      {assets.length > 0 && (
        <section className="studio-library">
          <header>
            <div><FileImage size={17} /><strong>历史任务</strong><span>{tasks.length}</span></div>
            <small>共 {assets.length} 个版本</small>
          </header>
          <div className="studio-library-grid">
            {tasks.map((asset) => {
              const rootId = asset.root_id || asset.id;
              const versionCount = assets.filter((item) => (item.root_id || item.id) === rootId).length;
              return (
                <button key={asset.id} onClick={() => onChoose(asset)} title={operationLabel(asset)} type="button">
                  <span className="studio-library-preview">
                    <img src={asset.url} alt={operationLabel(asset)} />
                    <em>{versionCount} 个版本</em>
                  </span>
                  <span className="studio-library-copy">
                    <strong>{operationLabel(asset)}</strong>
                    <small>{asset.width} × {asset.height} · {formatAssetTime(asset.created_at)}</small>
                  </span>
                </button>
              );
            })}
          </div>
        </section>
      )}
    </main>
  );
}

function ToolPanel({
  activeTool,
  busy,
  correctText,
  cutoutPreviewColor,
  cutoutPreviewImage,
  cutoutPreviewImageName,
  cutoutPreviewMode,
  current,
  enhanceMode,
  maskSelected,
  repairPrompt,
  selectedColor,
  upscaleScale,
  setCorrectText,
  setCutoutPreviewColor,
  setCutoutPreviewMode,
  setEnhanceMode,
  setRepairPrompt,
  setSelectedColor,
  setUpscaleScale,
  setVariantPrompt,
  setVariantStyle,
  svgDownload,
  variantPrompt,
  variantStyle,
  onClearMask,
  onUndoMask,
  onEnhance,
  onCutout,
  onCutoutPreviewImage,
  onRemoveCutoutPreviewImage,
  onUpscale,
  onRepair,
  onTextFix,
  onColor,
  onVariant,
  onVector,
}: {
  activeTool: ToolKey;
  busy: BusyKey | null;
  correctText: string;
  cutoutPreviewColor: string;
  cutoutPreviewImage: string | null;
  cutoutPreviewImageName: string;
  cutoutPreviewMode: CutoutPreviewMode;
  current: ImageResult;
  enhanceMode: AiTransformMode;
  maskSelected: boolean;
  repairPrompt: string;
  selectedColor: string;
  upscaleScale: 2 | 4;
  setCorrectText: (value: string) => void;
  setCutoutPreviewColor: (value: string) => void;
  setCutoutPreviewMode: (value: CutoutPreviewMode) => void;
  setEnhanceMode: (value: AiTransformMode) => void;
  setRepairPrompt: (value: string) => void;
  setSelectedColor: (value: string) => void;
  setUpscaleScale: (value: 2 | 4) => void;
  setVariantPrompt: (value: string) => void;
  setVariantStyle: (value: string) => void;
  svgDownload: string | null;
  variantPrompt: string;
  variantStyle: string;
  onClearMask: () => void;
  onUndoMask: () => void;
  onEnhance: () => void;
  onCutout: () => void;
  onCutoutPreviewImage: (file?: File) => void;
  onRemoveCutoutPreviewImage: () => void;
  onUpscale: () => void;
  onRepair: () => void;
  onTextFix: () => void;
  onColor: (mode: ColorEffectMode) => void;
  onVariant: () => void;
  onVector: () => void;
}) {
  const previewImageInput = useRef<HTMLInputElement>(null);
  const title = tools.find((tool) => tool.id === activeTool)!.label;
  const disabled = Boolean(busy);
  const upscaledWidth = current.width * upscaleScale;
  const upscaledHeight = current.height * upscaleScale;
  const upscaleTooLarge = upscaledWidth * upscaledHeight > 80_000_000;
  return (
    <section className="studio-tool-panel">
      <header><span>{tools.findIndex((tool) => tool.id === activeTool) + 1}</span><h2>{title}</h2></header>

      {activeTool === "enhance" && (
        <>
          <div className="studio-segments vertical">
            {enhanceModes.map((mode) => (
              <button className={mode.id === enhanceMode ? "active" : ""} key={mode.id} onClick={() => setEnhanceMode(mode.id)} type="button">
                {mode.label}{mode.id === enhanceMode && <Check size={14} />}
              </button>
            ))}
          </div>
          <button className="studio-primary" disabled={disabled} onClick={onEnhance} type="button"><Sparkles size={17} />开始处理</button>
        </>
      )}

      {activeTool === "cutout" && (
        <div className="studio-cutout-panel">
          {current.operation === "remove-background" ? (
            <div className="studio-cutout-ready">
              <span><Check size={16} /></span>
              <div><strong>透明 PNG</strong><small>抠图结果</small></div>
            </div>
          ) : (
            <div className="studio-cutout-start">
              <span><Scissors size={25} /></span>
              <button className="studio-primary" disabled={disabled} onClick={onCutout} type="button">
                <Scissors size={17} />一键智能抠图
              </button>
            </div>
          )}

          {current.operation === "remove-background" && (
            <>
              <div className="studio-preview-heading">
                <span><PaintBucket size={15} />预览背景</span>
                <em>仅预览</em>
              </div>
              <div className="studio-preview-backgrounds">
                {cutoutPreviewOptions.map((option) => (
                  <button
                    aria-pressed={cutoutPreviewMode === option.id}
                    className={cutoutPreviewMode === option.id ? "active" : ""}
                    key={option.id}
                    onClick={() => setCutoutPreviewMode(option.id)}
                    type="button"
                  >
                    <i className={`preview-swatch ${option.id}`} />
                    <span>{option.label}</span>
                  </button>
                ))}
              </div>

              <label
                className={cutoutPreviewMode === "custom" ? "studio-preview-color active" : "studio-preview-color"}
                onClick={() => setCutoutPreviewMode("custom")}
              >
                <input
                  aria-label="自定义预览背景颜色"
                  onChange={(event) => {
                    setCutoutPreviewColor(event.target.value);
                    setCutoutPreviewMode("custom");
                  }}
                  type="color"
                  value={cutoutPreviewColor}
                />
                <span><strong>自定义颜色</strong><small>{cutoutPreviewColor.toUpperCase()}</small></span>
              </label>

              <button
                className={cutoutPreviewMode === "image" ? "studio-preview-upload active" : "studio-preview-upload"}
                onClick={() => previewImageInput.current?.click()}
                type="button"
              >
                <ImagePlus size={17} />
                <span><strong>本地背景图</strong><small>{cutoutPreviewImageName || "选择图片"}</small></span>
              </button>
              <input
                accept="image/*"
                hidden
                onChange={(event) => {
                  onCutoutPreviewImage(event.target.files?.[0]);
                  event.currentTarget.value = "";
                }}
                ref={previewImageInput}
                type="file"
              />
              {cutoutPreviewImage && (
                <button className="studio-preview-remove" onClick={onRemoveCutoutPreviewImage} type="button">
                  <Trash2 size={14} />移除背景图
                </button>
              )}

              <a className="studio-primary studio-cutout-download" href={current.download_url}>
                <Download size={17} />下载透明 PNG
              </a>
            </>
          )}
        </div>
      )}

      {activeTool === "upscale" && (
        <div className="studio-upscale-panel">
          <div className="studio-segments studio-scale-options">
            {([2, 4] as const).map((scale) => (
              <button
                className={upscaleScale === scale ? "active" : ""}
                key={scale}
                onClick={() => setUpscaleScale(scale)}
                type="button"
              >
                <strong>{scale}×</strong>
                <small>{current.width * scale} × {current.height * scale}</small>
              </button>
            ))}
          </div>
          <div className={upscaleTooLarge ? "studio-output-size error" : "studio-output-size"}>
            <span>输出尺寸</span>
            <strong>{upscaledWidth} × {upscaledHeight} px</strong>
          </div>
          <button className="studio-primary" disabled={disabled || upscaleTooLarge} onClick={onUpscale} type="button">
            <Scaling size={17} />生成 {upscaleScale}× 版本
          </button>
        </div>
      )}

      {(activeTool === "repair" || activeTool === "text") && (
        <>
          <div className="studio-mask-tools">
            <span className={maskSelected ? "selected" : ""}><Brush size={15} />修复区域</span>
            <button onClick={onUndoMask} title="撤销画笔" type="button"><Undo2 size={16} /></button>
            <button onClick={onClearMask} title="清除选区" type="button"><RotateCcw size={16} /></button>
          </div>
          {activeTool === "repair" ? (
            <textarea maxLength={1000} onChange={(event) => setRepairPrompt(event.target.value)} placeholder="例如：补全缺失的右手，保持原有线稿风格" value={repairPrompt} />
          ) : (
            <input maxLength={300} onChange={(event) => setCorrectText(event.target.value)} placeholder="输入正确文字" value={correctText} />
          )}
          <button className="studio-primary" disabled={disabled || !maskSelected} onClick={activeTool === "repair" ? onRepair : onTextFix} type="button">
            <Brush size={17} />生成修正版
          </button>
        </>
      )}

      {activeTool === "color" && (
        <>
          <div className="studio-effect-grid">
            <button disabled={disabled} onClick={() => onColor("grayscale")} type="button"><Contrast size={17} />灰度</button>
            <button disabled={disabled} onClick={() => onColor("threshold")} type="button"><Contrast size={17} />黑白</button>
            <button disabled={disabled} onClick={() => onColor("invert")} type="button"><RotateCcw size={17} />反色</button>
          </div>
          <label className="studio-field-label">单色换色</label>
          <div className="studio-swatches">
            {colorSwatches.map((color) => (
              <button
                aria-label={`选择颜色 ${color}`}
                className={selectedColor === color ? "active" : ""}
                key={color}
                onClick={() => setSelectedColor(color)}
                style={{ backgroundColor: color }}
                type="button"
              />
            ))}
          </div>
          <button className="studio-primary" disabled={disabled} onClick={() => onColor("monochrome")} type="button"><Palette size={17} />应用颜色</button>
        </>
      )}

      {activeTool === "variant" && (
        <>
          <div className="studio-segments vertical">
            {variantStyles.map((style) => (
              <button className={variantStyle === style.id ? "active" : ""} key={style.id} onClick={() => setVariantStyle(style.id)} type="button">
                {style.label}{variantStyle === style.id && <Check size={14} />}
              </button>
            ))}
          </div>
          <textarea maxLength={1000} onChange={(event) => setVariantPrompt(event.target.value)} placeholder="可选：补充希望变化的内容" value={variantPrompt} />
          <button className="studio-primary" disabled={disabled} onClick={onVariant} type="button"><Shuffle size={17} />生成新版本</button>
        </>
      )}

      {activeTool === "vector" && (
        <div className="studio-single-action">
          <span><PenTool size={25} /></span>
          {svgDownload ? (
            <a className="studio-primary" href={svgDownload}><Download size={17} />下载 SVG</a>
          ) : (
            <button className="studio-primary" disabled={disabled || current.mime_type === "image/svg+xml"} onClick={onVector} type="button">生成 SVG</button>
          )}
        </div>
      )}
    </section>
  );
}

interface MaskCanvasHandle {
  clear: () => void;
  undo: () => void;
  toMaskFile: () => Promise<File>;
}

const MaskCanvas = forwardRef<MaskCanvasHandle, { asset: ImageResult; onSelectionChange: (selected: boolean) => void }>(
  function MaskCanvas({ asset, onSelectionChange }, ref) {
    const stageRef = useRef<HTMLDivElement>(null);
    const imageRef = useRef<HTMLImageElement>(null);
    const canvasRef = useRef<HTMLCanvasElement>(null);
    const drawing = useRef(false);
    const history = useRef<ImageData[]>([]);
    const [layout, setLayout] = useState({ left: 0, top: 0, width: 1, height: 1 });

    function syncLayout() {
      const stage = stageRef.current;
      if (!stage || !asset.width || !asset.height) return;
      const rect = stage.getBoundingClientRect();
      const scale = Math.min(rect.width / asset.width, rect.height / asset.height);
      const width = asset.width * scale;
      const height = asset.height * scale;
      setLayout({ left: (rect.width - width) / 2, top: (rect.height - height) / 2, width, height });
    }

    function clear() {
      const canvas = canvasRef.current;
      canvas?.getContext("2d")?.clearRect(0, 0, canvas.width, canvas.height);
      history.current = [];
      onSelectionChange(false);
    }

    function undo() {
      const canvas = canvasRef.current;
      const snapshot = history.current.pop();
      if (!canvas || !snapshot) return;
      canvas.getContext("2d")?.putImageData(snapshot, 0, 0);
      onSelectionChange(history.current.length > 0);
    }

    useImperativeHandle(ref, () => ({
      clear,
      undo,
      toMaskFile: () => new Promise<File>((resolve, reject) => {
        const selection = canvasRef.current;
        if (!selection || history.current.length === 0) {
          reject(new Error("请先涂抹修改区域"));
          return;
        }
        const mask = document.createElement("canvas");
        mask.width = asset.width;
        mask.height = asset.height;
        const context = mask.getContext("2d")!;
        context.fillStyle = "#000";
        context.fillRect(0, 0, mask.width, mask.height);
        context.globalCompositeOperation = "destination-out";
        context.drawImage(selection, 0, 0);
        mask.toBlob((blob) => {
          if (!blob) reject(new Error("无法生成修复遮罩"));
          else resolve(new File([blob], "mask.png", { type: "image/png" }));
        }, "image/png");
      }),
    }));

    useEffect(() => {
      clear();
      syncLayout();
      const observer = new ResizeObserver(syncLayout);
      if (stageRef.current) observer.observe(stageRef.current);
      return () => observer.disconnect();
    }, [asset.id]);

    function point(event: React.PointerEvent<HTMLCanvasElement>) {
      const canvas = canvasRef.current!;
      const rect = canvas.getBoundingClientRect();
      return {
        x: (event.clientX - rect.left) * (canvas.width / rect.width),
        y: (event.clientY - rect.top) * (canvas.height / rect.height),
      };
    }

    function start(event: React.PointerEvent<HTMLCanvasElement>) {
      const canvas = canvasRef.current!;
      const context = canvas.getContext("2d")!;
      history.current.push(context.getImageData(0, 0, canvas.width, canvas.height));
      drawing.current = true;
      canvas.setPointerCapture(event.pointerId);
      const next = point(event);
      context.beginPath();
      context.moveTo(next.x, next.y);
      context.lineTo(next.x + 0.01, next.y + 0.01);
      context.strokeStyle = "#ff3f4f";
      context.lineWidth = Math.max(14, Math.min(asset.width, asset.height) * 0.045);
      context.lineCap = "round";
      context.lineJoin = "round";
      context.stroke();
      onSelectionChange(true);
    }

    function move(event: React.PointerEvent<HTMLCanvasElement>) {
      if (!drawing.current) return;
      const context = canvasRef.current!.getContext("2d")!;
      const next = point(event);
      context.lineTo(next.x, next.y);
      context.stroke();
    }

    function stop(event: React.PointerEvent<HTMLCanvasElement>) {
      drawing.current = false;
      if (canvasRef.current?.hasPointerCapture(event.pointerId)) {
        canvasRef.current.releasePointerCapture(event.pointerId);
      }
    }

    return (
      <div className="studio-mask-canvas" ref={stageRef}>
        <img alt="待局部修改图片" onLoad={syncLayout} ref={imageRef} src={asset.url} />
        <canvas
          height={asset.height}
          onPointerCancel={stop}
          onPointerDown={start}
          onPointerMove={move}
          onPointerUp={stop}
          ref={canvasRef}
          style={layout}
          width={asset.width}
        />
      </div>
    );
  },
);

function Compare({ result, source }: { result: ImageResult; source: ImageResult }) {
  const [position, setPosition] = useState(50);
  return (
    <div className="studio-compare">
      <img src={result.url} alt="处理结果" />
      <div style={{ clipPath: `inset(0 ${100 - position}% 0 0)` }}><img src={source.url} alt="处理前" /></div>
      <i style={{ left: `${position}%` }} />
      <input aria-label="前后图片对比" max="100" min="0" onChange={(event) => setPosition(Number(event.target.value))} type="range" value={position} />
      <span>之前</span><span>当前</span>
    </div>
  );
}

function BusyOverlay({ label, fixed = false }: { label: string; fixed?: boolean }) {
  return <div className={fixed ? "studio-busy fixed" : "studio-busy"} role="status"><LoaderCircle className="spin" size={29} /><strong>{label}</strong></div>;
}

function operationLabel(asset: ImageResult): string {
  const aiMode = asset.metadata?.ai_mode;
  if (typeof aiMode === "string") {
    return enhanceModes.find((item) => item.id === aiMode)?.label
      ?? ({ "text-fix": "文字修正", "local-repair": "局部修复", recolor: "AI 换色", variant: "创意变体" }[aiMode] ?? "AI 编辑");
  }
  return {
    upload: "上传原图",
    generate: "AI 生成",
    edit: "AI 重绘",
    "ai-reconstruct": "AI 高清重绘",
    "remove-background": "透明背景",
    "color-effect": "颜色版本",
    vectorize: "SVG 矢量",
    restore: "本地高清",
    "extract-print": "图案提取",
    upscale: "保真放大",
  }[asset.operation] ?? "图片结果";
}

function busyLabel(busy: BusyKey): string {
  return {
    upload: "正在上传图片",
    generate: "正在生成新图案",
    enhance: "正在重建高清细节",
    cutout: "正在生成透明背景",
    upscale: "正在放大图片尺寸",
    repair: "正在修复选中区域",
    text: "正在修正文字",
    color: "正在生成颜色版本",
    variant: "正在生成新变体",
    vector: "正在生成 SVG 路径",
  }[busy];
}

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

function formatBytes(bytes: number): string {
  return bytes < 1024 * 1024
    ? `${Math.max(1, Math.round(bytes / 1024))} KB`
    : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatAssetTime(value?: string): string {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "最近";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function uniqueAssets(items: ImageResult[]): ImageResult[] {
  return [...new Map(items.map((asset) => [asset.id, asset])).values()];
}

function sortVersions(items: ImageResult[]): ImageResult[] {
  return [...items].sort((left, right) =>
    String(left.created_at ?? "").localeCompare(String(right.created_at ?? "")),
  );
}

export default SimpleApp;
