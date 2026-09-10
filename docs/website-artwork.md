# 网站品牌与配图

已接入的原创矢量资源：

- `frontend/public/brand-symbol.svg`：紫色叠纸图像标识，点缀暖金创意星芒，应用于登录、用户导航、管理后台和 favicon。
- `frontend/public/studio-collection.svg`：植物印花画稿、T 恤与杯子组成的 POD 主题插画，同一图案跨产品复用。源文件为可编辑 SVG，非 AI 生成位图。

AI 配图已获用户授权使用 imagegen CLI 备用流程，但本机未配置 `OPENAI_API_KEY`，因此尚未生成或接入。不要把占位文件改成图片扩展名或声称已生成。可在本地设置密钥后使用随 imagegen 技能提供的 `scripts/image_gen.py`；不将密钥加入仓库或对话。

最终生成稿（用于首页和登录配图，product-mockup）：

```text
Use case: product-mockup
Asset type: website artwork for a print-on-demand image editing studio
Primary request: A refined editorial still life showing a botanical print design
as a flat art print, on a folded cream cotton T-shirt, and on a ceramic mug.
The same original terracotta and lilac wildflower illustration must appear
consistently across all three products, with sage green leaves and warm ivory ink.
Scene/backdrop: seamless deep muted plum studio backdrop, softly lit with ample breathing room.
Style/medium: premium product photography, realistic cotton and unglazed ceramic texture,
restrained warm lighting, tactile paper, natural soft shadows, crisp print detail.
Composition/framing: balanced landscape composition with the three products fully visible,
slight overlap and depth, no clipped products, no busy props, art print upright behind the products.
Constraints: original artwork only; no text, no logos, no watermark, no invented UI.
```

在 PowerShell 中指定实际安装的脚本路径后运行（环境变量只需在本地配置，不打印）：

```powershell
# $imageGenScript 设置为安装的 imagegen/scripts/image_gen.py 完整路径
python $imageGenScript generate --model gpt-image-2 --prompt-file docs/website-artwork-prompt.txt --size 1536x1024 --quality high --output-format webp --out output/imagegen/pod-collection.webp
```

生成后必须检查印花一致性、产品完整性和文字污染。合格成品复制到 `frontend/public/`，将 `UserApp.tsx` 中对应的 `studio-collection.svg` 引用替换为真实成品路径，并重新构建检查布局。SVG Logo 继续保持清晰的矢量版本。
