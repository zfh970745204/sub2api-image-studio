import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import AdminApp from "./AdminApp";
import UserApp from "./UserApp";
import "./admin.css";
import "./user-app.css";

const RootApp = window.location.pathname.startsWith("/admin") ? AdminApp : UserApp;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RootApp />
  </StrictMode>,
);
