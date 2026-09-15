# 图片服务多协议适配

后台入口：**系统配置 → 图片服务**。配置组代码继续使用 `sub2api`，历史任务、加密密钥、配置版本和积分计费无需迁移。旧配置没有 `provider` 时按 `openai` 处理，保留原模型、地址和密钥。

## 已实现的协议

| 后台选项 / provider | 基础地址示例 | 生成及编辑能力 |
| --- | --- | --- |
| OpenAI / Sub2API 兼容 · `openai` | `https://api.openai.com/v1` 或自己的兼容网关 | GPT Image 文生图、多图编辑、透明遮罩重绘；DALL·E 3 文生图；DALL·E 2 方图和单图编辑 |
| OpenLux GPT Image · `openlux` | `https://api.openlux.ai/v1` | GPT Image 生图使用文档的 `format` 字段，编辑走 multipart `/images/edits` |
| Gemini / Nano Banana · `gemini` | `https://generativelanguage.googleapis.com/v1beta` | `generateContent` 文生图及多参考图；2.5 系列最多 3 图，3 系列最多 14 图；无透明遮罩参数 |
| 即梦 / 豆包 · `seedream` | `https://ark.cn-beijing.volces.com/api/v3` | Seedream 4 / 4.5 文生图及多图编辑，同走 JSON `/images/generations`；可用模型 ID 或推理接入点 ID |
| 硅基流动 · `siliconflow` | `https://api.siliconflow.cn/v1` | 文生图使用 `image_size`；Qwen-Image-Edit 使用 `image`，2509 支持 `image2` / `image3`；图片 URL 返回 |
| 阿里百炼 · `dashscope` | `https://dashscope.aliyuncs.com/api/v1` 或工作空间地域域名 | 原生同步多模态图片接口；千问文生图、千问编辑、万相 2.6 / 2.7；无 OpenAI 式透明遮罩参数 |
| BFL FLUX · `bfl` | `https://api.bfl.ai/v1` | 原生异步提交及轮询；FLUX.2 最多 8 张参考图，Kontext 1 张，1.1 文生图 |
| Stability AI · `stability` | `https://api.stability.ai/v2beta` | Core 文生图；Ultra / SD3.5 文生图和单图编辑；有遮罩时调用独立 Inpaint 服务 |

以上是实现的接口协议与模型系列，不表示任何服务商的所有模型都拥有相同能力。OpenAI 兼容网关可以使用自定义地址和模型名，但必须提供 Images API；仅提供 `/chat/completions`、Responses、fal、Replicate 或 Midjourney 提交接口的网关不能直接套用该协议。OpenLux 文档中的语音、视频和这些专有任务接口不在本次图片适配范围内。

## 配置步骤

1. 选择接口协议，再点击“填入接口示例”；协议切换本身不会覆盖已有地址或模型。
2. 根据账户已开通的服务调整模型和基础地址。地址只保留 API 前缀，不包含具体操作路径、查询参数或密钥。
3. 输入该服务的 API Key，保存并生效。留空会保留该线路已有密钥，因此更换服务商时应同时更换密钥。
4. 使用“启用多线路配置”添加独立线路和优先级。文生图专用模型与编辑专用模型应分别配置。
5. 保存后测试连接。测试不会生成图片，也不会消耗生图额度。模型列表或账户接口可验证认证；没有统一查询接口的协议仅检查接口可达，并明确提示不能据此判断密钥、模型权限和生成效果。

每条线路选择 `auto` 鉴权时：Gemini 用 `x-goog-api-key`，BFL 用 `x-key`，其余用 `Authorization: Bearer ...`。需要中转时可以显式改为 Bearer。

### OpenLux

| 使用模型 | 协议 | 基础地址 | 鉴权 |
| --- | --- | --- | --- |
| `gpt-image-2` | `openlux` | `https://api.openlux.ai/v1` | auto / Bearer |
| `gemini-3-pro-image-preview` | `gemini` | `https://api.openlux.ai/v1beta` | Bearer |
| `doubao-seedream-4-5-251128` | `seedream` | `https://api.openlux.ai/v1` | Bearer |

Gemini、Seedream 的后台选项提供“使用 OpenLux 中转”按钮。模型示例必须以账户当前可用列表为准，不保证所有地区或中转平台已开通。

OpenLux 的千问编辑示例要求公开的 `image` URL，其他 fal / Replicate 模型也有自己的请求结构；不应选择 GPT Image 协议来调用。当前千问可通过原生百炼或硅基流动线路使用。

## 任务行为

- 提交前检查编辑能力、参考图数量、遮罩支持；不兼容的线路自动跳过，不发送请求。所有线路都不支持时返回明确原因。参考图、遮罩不会被静默丢弃。
- 电商套图预先排除无法使用参考图的线路，每张图继续使用已有的产品一致性流程。
- 额度不足、限流和服务异常沿用现有线路故障切换。已收到成功提交响应，后续下载、解析或轮询失败时不自动重新提交，以免重复付费。BFL 轮询异常只重试 GET；整个调用共享一个截止时间。
- BFL 使用返回的区域查询地址，并限制携带密钥的目标域名。任务 ID 进入执行结果或失败记录。工作进程终止后沿用现有超时退款机制；本次没有增加跨进程恢复远端未完成任务的功能。
- PNG / JPEG / WebP 均按实际图片内容验证和转换。JPEG 不支持透明度，必要时合成白底。图片结果下载不会携带 API Key，DNS 解析后固定公网 IP，逐次检查重定向并限制响应体大小。
- 默认模型继续保留 `gpt-image-2`。各模型仅接收对应协议支持的画质与输出格式字段。

## 尺寸与编辑限制

原生服务并不都支持任意宽高：Gemini 转为宽高比与 1K / 2K / 4K 档位（2.5 不发送高分辨率字段）；Seedream 4+ 至少约 2K 像素总量；DALL·E 使用固定尺寸；千问大图按协议边界适配；Stability 和部分 FLUX 模型由服务决定像素尺寸。硅基流动千问编辑不支持 `image_size`，不会发送该字段。

印花提取仍按用户选择的最终画布尺寸输出，并保留原有透明 / 不透明处理。普通生图返回模型实际输出尺寸，不通过拉伸伪造原生清晰度。万相不支持透明参考图，会跳过该线路；完全不透明的 RGBA 图只移除冗余 alpha 通道。Stability 单图编辑强度使用 0.65；局部重绘把编辑器的 alpha 遮罩转换为白色编辑、黑色保留，并将 `grow_mask` 设为 0，避免额外扩大选区。

## 开发与验证

- 统一客户端：`backend/app/image_gateway.py`；兼容导出保留在 `app/sub2api.py`。
- 能力及配置类型：`backend/app/image_provider_types.py`；图片下载与验证：`backend/app/image_download.py`。
- 后台协议示例：`frontend/src/image-providers.ts`。
- 协议测试使用 `httpx.MockTransport` 和程序生成的小图，不调用真实收费服务。覆盖请求格式、遮罩转换、参考图、URL / base64 / 二进制结果、压缩响应、私网及重定向拦截、FLUX 轮询和防止重复提交。
- 上线前应为实际购买的线路填写密钥，并以真实任务验证模型权限和画质；本次未做付费接口实测或部署。

## 核对的文档

- [OpenLux GPT Image 生图](https://doc.openlux.ai/en/reference/v1?op=post-v1-images-generations&leaf=453199915)
- [OpenLux GPT Image 编辑](https://doc.openlux.ai/en/reference/v1?op=post-v1-images-edits&leaf=446294920)
- [OpenLux Gemini](https://doc.openlux.ai/en/reference/gemini-v1beta?op=post-v1beta-models-gemini-3-pro-image-preview-generatecontent&leaf=379838953)
- [OpenLux Seedream 4.5](https://doc.openlux.ai/en/reference/v1?op=post-v1-images-generations&leaf=478862844)
- [OpenAI Image generation](https://developers.openai.com/api/docs/guides/image-generation)
- [SiliconFlow Image Generation](https://docs.siliconflow.cn/docs/api/images-generations-post)
- [千问生图](https://help.aliyun.com/en/model-studio/qwen-image-api)
- [万相生图和编辑](https://help.aliyun.com/en/model-studio/wan-image-generation-and-editing-api-reference)
- [BFL 图片生成](https://docs.bfl.ai/quick_start/generating_images) / [多图编辑](https://docs.bfl.ai/flux_2/flux2_image_editing)
- [Stability API](https://platform.stability.ai/docs/api-reference)
