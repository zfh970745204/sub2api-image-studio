# 网站品牌与配图

当前使用的 Logo 是可编辑矢量资源 `frontend/public/brand-symbol.svg`。三张页面配图是本次实际生成并经检查的 WebP 位图，均已随前端发布：

| 页面 | 文件 | 尺寸与用途 |
| --- | --- | --- |
| 登录 | `frontend/public/brand/login-studio-v3.webp` | 真实黑色印花 T 恤产品摄影，右侧主体、左侧留白 |
| 注册 | `frontend/public/brand/register-studio-v3.webp` | 真实陶瓷杯与帆布袋产品摄影，体现 POD 商品场景 |
| 首页 | `frontend/public/brand/home-studio-v3.webp` | 真实服装、杯子与印刷品的电商产品摄影 |

## 落地页案例

落地页“功能效果”区域现在提供四个可切换案例，图片同时提供缩略图和大图资源，浏览器会按当前展示尺寸选择合适的资源，并延迟加载尚未进入视口的案例：

| 案例 | 输入 | 结果 | 展示重点 |
| --- | --- | --- | --- |
| 印花提取 | `shirt-source-v3.webp` | `shirt-print-v3.png` | 从真实黑色 T 恤提取完整印花，输出无背景透明 PNG；棋盘格只表示透明区域，不是洋红色底色。 |
| 电商主图 | `mug-source-v3.webp` | `mug-commerce-v3.webp` | 同一只杯子的真实商品视觉前后对照。 |
| 高清重绘 | `shirt-source-v3.webp` | `shirt-redraw-v3.webp` | 同一张 T 恤照片的对应结果，保留人物、衣服、文字和构图，只增强分辨率、边缘与细节。 |
| AI 生图 | 文字提示卡 | `ai-generate-v3.webp` | 从文字描述生成完整 POD 产品视觉，展示卫衣、杯子与配套印花的真实产品摄影效果。 |

高清重绘的前后图必须成对使用同一输入，避免用户误以为是两个不同案例。AI 生图没有“处理前图片”，因此左侧展示实际提交的文字提示，右侧展示生成结果。

页面效果示例使用真实输入与处理结果：`shirt-source-v3.webp` -> `shirt-print-v3.png`、`shirt-source-v3.webp` -> `shirt-redraw-v3.webp`，以及 CC BY-SA 4.0 的真实杯子照片 `mug-source-v3.webp` -> `mug-commerce-v3.webp`。杯子原图来源：Runologe, Wikimedia Commons, https://commons.wikimedia.org/wiki/File:A_mug_with_the_runes_of_a_page_of_the_Codex_Runicus_-_Tasse_mit_Runen_einer_Seite_des_Codex_Runicus.jpg 。

生成使用 imagegen 的 CLI 备用流程与 `gpt-image-2`，高质量 WebP 输出。精确的三条提示和输出文件名保存在 `docs/brand-artwork-prompts.jsonl`；生成原件在本地 `output/imagegen/brand/`，该目录被忽略，不会进入仓库。项目中的发布版本已经经过压缩，约 180–190 KB/张。

## 后台替换

超级管理员进入“系统配置 → 网站名称与品牌配图”，可以直接修改网站名称和各图片地址，也可分别上传 Logo、登录页、注册页和首页的 PNG/JPEG/WebP。上传内容会被校正方向、限制在 1920 px 内并转为 WebP；公开路由使用不可变缓存。品牌配置只公开上述白名单字段，绝不包含接口、邮件或存储配置。

允许填写 HTTPS 外链或本地 `/brand/`、`/api/v1/site/media/` 地址。不要填写带密码的 URL、`data:` URL 或不受控的脚本 URL。

## 创作规范

```text
Use case: stylized-concept
Asset type: brand artwork for a professional image creation studio
Primary request: original gallery-quality glass and sculptural materials for a creative studio.
Composition/framing: usable negative space for UI copy; portrait for authentication, landscape for home.
Lighting/mood: precise studio light, rich material detail, restrained contrast.
Constraints: no text, logos, watermark, people, product mockups or opaque UI cards.
```
