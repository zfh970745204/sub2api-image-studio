import { useState } from "react";
import { FileImage } from "lucide-react";

export function ImageThumbnail({ id, alt = "图片预览", vector = false }: { id: string | null; alt?: string; vector?: boolean }) {
  const [failed, setFailed] = useState<string | null>(null);
  if (!id || vector || failed === id) return <FileImage size={25} aria-label="暂无缩略图" />;
  return <img src={`/api/v1/assets/${encodeURIComponent(id)}/thumbnail`} alt={alt} loading="lazy" decoding="async" onError={() => setFailed(id)} />;
}
