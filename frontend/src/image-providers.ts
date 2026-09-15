export const IMAGE_PROVIDERS = [
  { id: "openai", name: "OpenAI / Sub2API 兼容", url: "https://api.openai.com/v1", model: "gpt-image-2", hint: "GPT Image 支持文生图、参考图和遮罩编辑；DALL·E 3 仅支持文生图。其他兼容网关可填写自己的地址。" },
  { id: "openlux", name: "OpenLux · GPT Image", url: "https://api.openlux.ai/v1", model: "gpt-image-2", hint: "适配 OpenLux 的 GPT Image 生图与编辑。通过 OpenLux 调用 Gemini 或即梦时，请选对应协议并使用下方中转预设。" },
  { id: "gemini", name: "Google Gemini / Nano Banana", url: "https://generativelanguage.googleapis.com/v1beta", model: "gemini-3-pro-image-preview", hint: "支持文生图及多参考图；2.5 系列最多 3 图，3 系列最多 14 图。尺寸转换为比例与 1K / 2K / 4K 档位；不支持遮罩重绘。" },
  { id: "seedream", name: "即梦 / 豆包 Seedream", url: "https://ark.cn-beijing.volces.com/api/v3", model: "doubao-seedream-4-5-251128", hint: "Seedream 4 / 4.5 支持文生图与多图编辑，默认至少 2K；模型也可填写已开通的推理接入点 ID。不支持遮罩重绘。" },
  { id: "siliconflow", name: "硅基流动 SiliconFlow", url: "https://api.siliconflow.cn/v1", model: "Qwen/Qwen-Image", hint: "文生图与编辑模型请分线路配置。Qwen/Qwen-Image-Edit-2509 支持最多 3 张参考图，编辑尺寸由模型决定；不支持遮罩重绘。" },
  { id: "dashscope", name: "阿里百炼 · 千问 / 万相", url: "https://dashscope.aliyuncs.com/api/v1", model: "qwen-image-2.0-pro", hint: "原生同步多模态接口，支持千问 2.0 与编辑模型、万相 2.6 / 2.7；可填写对应地域或工作空间域名。旧版万相异步接口不属于此协议。" },
  { id: "bfl", name: "Black Forest Labs · FLUX", url: "https://api.bfl.ai/v1", model: "flux-2-pro", hint: "原生异步生图。FLUX.2 支持最多 8 张参考图，Kontext 支持 1 张；自动等待生成结果，不支持遮罩重绘。" },
  { id: "stability", name: "Stability AI · Stable Diffusion", url: "https://api.stability.ai/v2beta", model: "sd3.5-large", hint: "支持 SD3.5 / Ultra 文生图和单图编辑，Core 仅支持文生图。局部重绘使用专用 Inpaint 服务；尺寸由模型决定，单图编辑强度为 0.65。" },
] as const;

export function imageProvider(value: unknown) {
  return IMAGE_PROVIDERS.find((provider) => provider.id === value) || IMAGE_PROVIDERS[0];
}
