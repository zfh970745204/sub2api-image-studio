import { lazy, StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { SiteBrandingProvider } from "./SiteBranding";
import "./theme.css";
import "./busy-dialog.css";
import "./admin.css";
import "./user-app.css";
import "./workspace.css";
import "./experience.css";
import "./design.css";
import "./editor.css";
import "./landing.css";
import "./polish.css";

const AdminApp = lazy(() => import("./AdminApp"));
const UserApp = lazy(() => import("./UserApp"));
const LandingPage = lazy(() => import("./LandingPage").then((module) => ({ default: module.LandingPage })));
const RootApp = window.location.pathname === "/" ? LandingPage : window.location.pathname.startsWith("/admin") ? AdminApp : UserApp;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <SiteBrandingProvider><Suspense fallback={<div className="studio-boot-loading" role="status"><span />正在加载页面…</div>}><RootApp /></Suspense></SiteBrandingProvider>
  </StrictMode>,
);
