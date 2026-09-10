# 网站品牌与配图

已接入的原创矢量资源：

- `frontend/public/brand-symbol.svg`：紫色叠纸图像标识，点缀暖金创意星芒，应用于登录、用户导航、管理后台和 favicon。
- `frontend/public/studio-atmosphere.svg`：登录页深色光谱雕塑，银色与淡紫反射面、细线轨道和留白；页面文字独立于配图，可随屏幕自适应。
- `frontend/public/studio-prism.svg`：首页悬浮玻璃画框和银色流动形体，与登录页保持同一视觉方向。桌面左右布局，手机上下布局。
- 原植物产品插画 `studio-collection.svg` 不再用于登录页和首页。

本次沿用项目可编辑 SVG 体系，已接入的是原创矢量资源，非 AI 位图。已检查 imagegen 可用性：本机会话没有内置生成工具，也未配置 `OPENAI_API_KEY` 或项目 Sub2API 凭据，因此没有实际调用图片生成接口。服务器上的接口配置不会自动出现在本地开发环境。

后续升级为渲染位图时的配图规范（与当前视觉一致，stylized-concept）：

```text
Use case: stylized-concept
Asset type: brand artwork for a professional image creation studio
Primary request: A sculptural folded silver ribbon floating within translucent glass image frames.
Scene/backdrop: deep charcoal blue, subtle atmospheric light, quiet gallery space.
Style/medium: refined studio rendering, smooth metallic reflections, restrained pearl and lilac highlights.
Composition/framing: one complete sculptural form, generous negative space, clean silhouette;
landscape for home, portrait with clear space above for login copy.
Lighting/mood: broad softbox light, subtle rim light, delicate contact shadow, understated and precise.
Constraints: original artwork; no text, logos, watermark, cartoons, flowers, clothing or mugs.
```

在 PowerShell 中指定实际安装的脚本路径后运行（环境变量只需在本地配置，不打印）：

```powershell
# $imageGenScript 设置为安装的 imagegen/scripts/image_gen.py 完整路径
python $imageGenScript generate --model gpt-image-2 --prompt-file docs/website-artwork-prompt.txt --size 1536x1024 --quality high --output-format webp --out output/imagegen/studio-prism.webp
```

生成后必须检查形体完整性、留白、明暗与文字可读性。合格成品复制到 `frontend/public/`，替换 `UserApp.tsx` 中对应资源并重新构建检查桌面和手机布局；Logo 保持矢量版本。当前两个 SVG 无外部依赖，随前端构建直接部署。
