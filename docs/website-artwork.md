# 网站品牌与配图

当前使用的 Logo 是可编辑矢量资源 `frontend/public/brand-symbol.svg`。三张页面配图是本次实际生成并经检查的 WebP 位图，均已随前端发布：

| 页面 | 文件 | 尺寸与用途 |
| --- | --- | --- |
| 登录 | `frontend/public/brand/login-art.webp` | 纵向银色丝带与玻璃材质，预留文案空间 |
| 注册 | `frontend/public/brand/register-art.webp` | 纵向珍珠、纸艺和玻璃材质，用于独立注册体验 |
| 首页 | `frontend/public/brand/home-art.webp` | 横向玻璃雕塑，左侧留出首页创作入口文案 |

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
