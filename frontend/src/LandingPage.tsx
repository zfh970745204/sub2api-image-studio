import { ArrowDown, ArrowRight, Check, ChevronRight, Layers3, ScanLine, Sparkles, WandSparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { useSiteBranding } from "./SiteBranding";
import { api, ApiError, type UserSummary } from "./user-api";

const demos = [
  { name: "印花提取", icon: ScanLine, title: "好设计，不必困在产品里。", description: "从衣服、杯子、帆布袋等产品图片中还原平面图案，保留设计与色彩，输出透明 PNG。", before: "真实 T 恤原图", after: "透明印花 PNG", beforeImage: "/brand/shirt-source-v3.webp", afterImage: "/brand/shirt-print-v3.png", tool: "ai.extract_print" },
  { name: "电商主图", icon: Layers3, title: "同一件产品，形成一整组主图。", description: "从真实产品照片生成干净的商品展示图，保持产品外观、印花与材质在每一张图里一致。", before: "真实杯子原图", after: "电商主图", beforeImage: "/brand/mug-source-v3.webp", afterImage: "/brand/mug-commerce-v3.webp", tool: "ai.ecommerce" },
  { name: "高清重绘", icon: WandSparkles, title: "熟悉的设计，更清晰的细节。", description: "修复模糊和边缘锯齿，提升原图清晰度。保留主体、背景和构图，继续打磨已有作品。", before: "原始产品照片", after: "高清重绘结果", beforeImage: "/brand/mug-source-v3.webp", afterImage: "/brand/mug-commerce-v3.webp", tool: "ai.redraw" },
];

function DemoArt({ mode, result = false }: { mode: number; result?: boolean }) {
  const item = demos[mode];
  const src = result ? item.afterImage : item.beforeImage;
  return <div className={`landing-demo-art ${result && mode === 0 ? "checker" : ""}`}><img src={src} alt={result ? item.after : item.before} loading="lazy" /></div>;
}

export function LandingPage() {
  const branding = useSiteBranding();
  const [user, setUser] = useState<UserSummary | null>(null);
  const [session, setSession] = useState<"checking" | "guest" | "member" | "unknown">("checking");
  const [registration, setRegistration] = useState(false);
  const [demo, setDemo] = useState(0);
  useEffect(() => {
    let active = true;
    let revision = 0;
    function refresh() {
      const current = ++revision;
      void api.currentUser().then(({ user: value }) => {
        if (active && current === revision) { setUser(value); setSession("member"); }
      }).catch((error: unknown) => {
        if (active && current === revision) {
          setSession(error instanceof ApiError && error.status === 401 ? "guest" : "unknown");
          setUser(null);
        }
      });
      void api.authOptions().then((value) => { if (active && current === revision) setRegistration(value.registration_enabled); }).catch(() => undefined);
    }
    const onVisibility = () => { if (document.visibilityState === "visible") refresh(); };
    refresh();
    window.addEventListener("focus", refresh);
    window.addEventListener("pageshow", refresh);
    document.addEventListener("visibilitychange", onVisibility);
    return () => { active = false; window.removeEventListener("focus", refresh); window.removeEventListener("pageshow", refresh); document.removeEventListener("visibilitychange", onVisibility); };
  }, []);

  const guest = session === "guest";
  const startHref = guest ? registration ? "/register" : "/login" : "/app/studio";
  const startLabel = guest ? registration ? "创建账号，开始创作" : "登录并开始创作" : "开始创作";
  const brand = <a className="landing-brand" href="/" aria-label={`${branding.site_name} · 网站首页`}><img src={branding.logo_url} width="34" height="34" alt="" /><strong>{branding.site_name}</strong></a>;
  return <main className="landing-page">
    <nav className="landing-nav" aria-label="网站导航">{brand}<div className="landing-nav-sections"><a href="#effects">功能效果</a><a href="#workflow">使用流程</a></div><div className="landing-account">
      {session === "member" && <span className="landing-greeting">你好，{user?.display_name}</span>}
      {guest ? <><a href="/login">登录</a>{registration && <a className="landing-button small" href="/register">开始使用<ArrowRight size={15} /></a>}</> : <a className="landing-button small" href="/app">进入工作台<ArrowRight size={15} /></a>}
    </div></nav>
    <section className="landing-hero">
      <div className="landing-hero-copy"><span className="landing-eyebrow"><i />为创作者而设计的图片工作台</span><h1>把一张图片，<br />变成<span>下一件作品。</span></h1><p>提取喜欢的印花，修复模糊的细节，<br className="landing-desktop-break" />让脑海里的灵感，拥有清晰的模样。</p><div className="landing-hero-actions"><a className="landing-button" href={startHref}>{startLabel}<ArrowRight size={18} /></a><a className="landing-text-link" href="#effects">看看效果<ArrowDown size={16} /></a></div><div className="landing-benefits"><span><Check size={14} />原图与结果同步对比</span><span><Check size={14} />每一步都有独立版本</span></div></div>
      <div className="landing-hero-visual custom">
        <img className="landing-custom-art" src={branding.home_image_url} alt={`${branding.site_name} 产品创作展示`} fetchPriority="high" decoding="async" />
      </div>
    </section>
    <div className="landing-capabilities"><span>从灵感到可用素材</span><div><span>AI 图片生成</span><i /><span>产品印花提取</span><i /><span>高清重绘</span><i /><span>透明 PNG</span><i /><span>版本管理</span></div></div>
    <section className="landing-effects" id="effects">
      <header className="landing-section-heading"><div><span className="landing-eyebrow">每一处改变，看得见</span><h2>让图片更接近你的想象。</h2></div><p>减少重复操作，<br />把时间留给真正的创作。</p></header>
      <div className="landing-effect-layout"><div className="landing-effect-copy"><div className="landing-demo-tabs" role="tablist" aria-label="功能效果">{demos.map((item, index) => <button key={item.name} id={`demo-tab-${index}`} aria-controls="landing-demo-panel" type="button" role="tab" aria-selected={demo === index} tabIndex={demo === index ? 0 : -1} onClick={() => setDemo(index)} onKeyDown={(event) => {
        if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === "Home" ? 0 : event.key === "End" ? demos.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + demos.length) % demos.length;
        setDemo(next); document.getElementById(`demo-tab-${next}`)?.focus();
      }}><item.icon size={16} />{item.name}</button>)}</div><span className="landing-effect-number">0{demo + 1} / THE DETAILS MATTER</span><h3>{demos[demo].title}</h3><p>{demos[demo].description}</p><a className="landing-text-link" href={guest ? startHref : `/app/studio?tool=${demos[demo].tool}`}>试试{demos[demo].name}<ChevronRight size={16} /></a><small>以下为真实图片处理样例，实际效果会随原图与参数变化。</small></div>
        <div className="landing-comparison" id="landing-demo-panel" role="tabpanel" aria-labelledby={`demo-tab-${demo}`}><figure><figcaption><i />{demos[demo].before}</figcaption><DemoArt mode={demo} /></figure><span className="landing-comparison-arrow"><ArrowRight size={20} /></span><figure><figcaption><i />{demos[demo].after}</figcaption><DemoArt mode={demo} result /></figure></div>
      </div>
    </section>
    <section className="landing-workflow" id="workflow"><header className="landing-section-heading"><div><span className="landing-eyebrow">简单三步，开始创作</span><h2>流程更轻，创作更自由。</h2></div><Sparkles size={30} strokeWidth={1.2} /></header><div className="landing-steps">{[
      ["01", "放入你的灵感", "上传已有图片，或描述一张你想创作的新图。"],
      ["02", "选择合适的工具", "提取、重绘或精修，提交前确认质量与积分报价。"],
      ["03", "带走满意的作品", "并排查看前后细节，下载结果，或沿着版本继续创作。"],
    ].map(([number, title, description]) => <article key={number}><span>{number}</span><h3>{title}</h3><p>{description}</p></article>)}</div></section>
    <section className="landing-closing"><div><span className="landing-eyebrow">下一件作品，从这里开始</span><h2>给灵感一个落地的地方。</h2></div><a className="landing-button" href={startHref}>{startLabel}<ArrowRight size={18} /></a></section>
    <footer className="landing-footer">{brand}<span>让创作简单，让细节出色。</span><a href="/app">进入工作台<ArrowRight size={14} /></a></footer>
  </main>;
}
