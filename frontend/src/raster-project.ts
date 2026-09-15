import { layerLimit, type LayerInfo } from "./raster-editor";

// Length-prefixed UTF-8 manifest followed by lossless PNGs, bottom layer first.
// No base64 expansion; metadata and decoded memory are bounded on both ends.
export type ProjectManifest = { version: 1; width: number; height: number; activeId: string; layers: (LayerInfo & { byteLength: number })[] };
export function encodeProject(manifest: ProjectManifest, blobs: Blob[]) {
  const header = new TextEncoder().encode(JSON.stringify(manifest));
  const size = new Uint8Array(4); new DataView(size.buffer).setUint32(0, header.length, true);
  return new Blob([size, header, ...blobs], { type: "application/octet-stream" });
}
export async function decodeProject(blob: Blob, width: number, height: number) {
  if (blob.size < 4 || blob.size > 128 * 1024 * 1024) throw new Error("图层文件大小无效");
  const length = new DataView(await blob.slice(0, 4).arrayBuffer()).getUint32(0, true);
  if (length > 16384 || length < 2 || length + 4 >= blob.size) throw new Error("图层文件头无效");
  const manifest = JSON.parse(await blob.slice(4, 4 + length).text()) as ProjectManifest;
  if (manifest.version !== 1 || manifest.width !== width || manifest.height !== height || !Array.isArray(manifest.layers) || !manifest.layers.length || manifest.layers.length > layerLimit(width, height)) throw new Error("图层尺寸或数量无效");
  const blobs: Blob[] = [], ids = new Set<string>(); let offset = 4 + length;
  for (const layer of manifest.layers) {
    if (!layer || typeof layer.id !== "string" || !/^[\w-]{1,64}$/.test(layer.id) || ids.has(layer.id) || typeof layer.name !== "string" || !layer.name.trim() || layer.name.length > 40 || typeof layer.visible !== "boolean" || typeof layer.locked !== "boolean" || !Number.isInteger(layer.opacity) || layer.opacity < 0 || layer.opacity > 100 || !Number.isInteger(layer.byteLength) || layer.byteLength < 1 || offset + layer.byteLength > blob.size) throw new Error("图层数据无效");
    ids.add(layer.id); blobs.push(blob.slice(offset, offset + layer.byteLength, "image/png")); offset += layer.byteLength;
  }
  if (offset !== blob.size || !ids.has(manifest.activeId)) throw new Error("图层文件不完整");
  return { manifest, blobs };
}
