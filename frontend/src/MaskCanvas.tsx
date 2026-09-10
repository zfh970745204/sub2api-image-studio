import { forwardRef, useEffect, useImperativeHandle, useRef, useState, type PointerEvent } from "react";
import { Brush, Undo2, Trash2 } from "lucide-react";

type Point = { x: number; y: number };
type Stroke = { points: Point[]; width: number };
export interface MaskCanvasHandle { exportMask(): Promise<File | null>; }

/** The overlay never reads remote image pixels, so private R2 images need no canvas CORS. */
export const MaskCanvas = forwardRef<MaskCanvasHandle, {
  width: number;
  height: number;
  disabled?: boolean;
  onChange: () => void;
}>(function MaskCanvas({ width, height, disabled, onChange }, ref) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const strokes = useRef<Stroke[]>([]);
  const drawing = useRef(false);
  const [count, setCount] = useState(0);
  const [brush, setBrush] = useState(32);
  const scale = Math.min(1, 1536 / Math.max(width, height));
  const canvasWidth = Math.max(1, Math.round(width * scale));
  const canvasHeight = Math.max(1, Math.round(height * scale));

  function paint() {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = "#8b5cf6";
    ctx.fillStyle = "#8b5cf6";
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    for (const stroke of strokes.current) {
      const start = stroke.points[0];
      ctx.lineWidth = stroke.width * canvas.width;
      ctx.beginPath();
      ctx.arc(start.x * canvas.width, start.y * canvas.height, ctx.lineWidth / 2, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.moveTo(start.x * canvas.width, start.y * canvas.height);
      for (const point of stroke.points.slice(1)) ctx.lineTo(point.x * canvas.width, point.y * canvas.height);
      ctx.stroke();
    }
  }

  useEffect(() => { paint(); }, [canvasWidth, canvasHeight]);

  useImperativeHandle(ref, () => ({
    async exportMask() {
      if (!strokes.current.length || !canvasRef.current) return null;
      const output = document.createElement("canvas");
      output.width = width;
      output.height = height;
      const context = output.getContext("2d");
      if (!context) throw new Error("浏览器无法创建遮罩，请上传透明 PNG 遮罩。");
      context.fillStyle = "#000";
      context.fillRect(0, 0, width, height);
      context.globalCompositeOperation = "destination-out";
      context.drawImage(canvasRef.current, 0, 0, width, height);
      const blob = await new Promise<Blob | null>((resolve) => output.toBlob(resolve, "image/png"));
      output.width = output.height = 0;
      if (!blob) throw new Error("遮罩导出失败，请减少图片尺寸后重试。");
      return new File([blob], "edit-mask.png", { type: "image/png" });
    },
  }));

  function point(event: PointerEvent<HTMLCanvasElement>): Point {
    const rect = event.currentTarget.getBoundingClientRect();
    return { x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height };
  }

  function update() { setCount(strokes.current.length); paint(); onChange(); }

  return <>
    <canvas
      aria-label="在图片上涂抹需要修改的区域"
      className="user-mask-canvas"
      aria-disabled={disabled}
      height={canvasHeight}
      width={canvasWidth}
      ref={canvasRef}
      onPointerDown={(event) => {
        if (disabled || event.button !== 0) return;
        event.currentTarget.setPointerCapture(event.pointerId);
        drawing.current = true;
        strokes.current.push({ points: [point(event)], width: brush / event.currentTarget.getBoundingClientRect().width });
        update();
      }}
      onPointerMove={(event) => {
        if (!drawing.current) return;
        strokes.current[strokes.current.length - 1].points.push(point(event));
        paint();
      }}
      onPointerUp={() => { drawing.current = false; }}
      onPointerCancel={() => { drawing.current = false; }}
      onLostPointerCapture={() => { drawing.current = false; }}
    />
    <div className="user-mask-toolbar">
      <Brush size={16} /><label>画笔<input aria-label="画笔大小" disabled={disabled} type="range" min="8" max="100" value={brush} onChange={(event) => setBrush(Number(event.target.value))} /></label>
      <button aria-label="撤销上一笔" disabled={disabled || !count} onClick={() => { strokes.current.pop(); update(); }} type="button"><Undo2 size={16} /></button>
      <button aria-label="清空涂抹" disabled={disabled || !count} onClick={() => { strokes.current = []; update(); }} type="button"><Trash2 size={16} /></button>
    </div>
  </>;
});
