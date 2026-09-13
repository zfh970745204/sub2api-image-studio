import { InfoHint } from "./InfoHint";
import type { Operation } from "./user-api";

export function EcommerceShotPlan({ plan, count }: { plan: Operation["ecommerce_plan"]; count: number }) {
  if (!plan) return null;
  return <section className="studio-shot-plan" aria-label="套图内容">
    <header><strong>{count === 1 ? "本次主图" : "本套内容"}</strong><InfoHint label="套图生成说明">每张按不同用途生成，统一产品外形、颜色、印花与文字。建议上传同一商品的不同角度原图；无法确认的背面和结构不会主动编造。</InfoHint></header>
    <ol>{plan.shots.slice(0, count).map((shot, index) => <li key={shot.code}><span>{String(index + 1).padStart(2, "0")}</span>{shot.label}<InfoHint label={`${shot.label}说明`}>{shot.description}</InfoHint></li>)}</ol>
  </section>;
}
