import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import AdminApp from "./AdminApp";
import UserApp from "./UserApp";
import { SiteBrandingProvider } from "./SiteBranding";
import "./admin.css";
import "./user-app.css";
import "./workspace.css";
import "./experience.css";
import "./design.css";
import "./editor.css";
import "./landing.css";

const RootApp = window.location.pathname.startsWith("/admin") ? AdminApp : UserApp;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <SiteBrandingProvider><RootApp /></SiteBrandingProvider>
  </StrictMode>,
);
