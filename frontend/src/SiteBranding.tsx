import { createContext, type ReactNode, useContext, useEffect, useState } from "react";

export const defaultBranding = {
  site_name: "Sub2Image", logo_url: "/brand-symbol.svg",
  login_image_url: "/brand/login-studio-v3.webp", register_image_url: "/brand/register-studio-v3.webp",
  home_image_url: "/brand/home-studio-v3.webp",
};
const BrandingContext = createContext(defaultBranding);
export const useSiteBranding = () => useContext(BrandingContext);

export function SiteBrandingProvider({ children }: { children: ReactNode }) {
  const [branding, setBranding] = useState(defaultBranding);
  useEffect(() => {
    let active = true;
    const reload = () => { void fetch("/api/v1/site").then(async (response) => {
      if (!response.ok) return;
      const values = await response.json() as Partial<typeof defaultBranding>;
      if (active) setBranding({ ...defaultBranding, ...values });
    }).catch(() => undefined); };
    reload();
    window.addEventListener("site-branding-updated", reload);
    return () => { active = false; window.removeEventListener("site-branding-updated", reload); };
  }, []);
  useEffect(() => {
    document.title = `${branding.site_name} · 图片创作工作台`;
    const icon = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
    if (icon) icon.href = branding.logo_url;
  }, [branding]);
  return <BrandingContext.Provider value={branding}>{children}</BrandingContext.Provider>;
}
