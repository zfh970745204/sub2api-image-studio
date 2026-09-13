"""One listing, complementary shots, a single product identity."""

from dataclasses import dataclass

PLAN_VERSION = "listing-set-v1"


@dataclass(frozen=True, slots=True)
class ListingShot:
    code: str
    label: str
    description: str
    direction: str


LISTING_SHOTS = (
    ListingShot(
        "hero",
        "商品主图",
        "干净背景，完整展示商品",
        "Catalog hero: show the entire product, uncropped, on a seamless white or very light "
        "neutral background with a restrained contact shadow. Product fills 75-85% of the "
        "frame. No room, tabletop styling, hands, props or decorative scene.",
    ),
    ListingShot(
        "lifestyle",
        "使用场景",
        "真实环境，展示商品用途",
        "Environmental lifestyle: show the product in a believable use setting appropriate "
        "to its visible function. Use a wider environmental composition; product occupies "
        "35-55% of the frame. Show contextual activity rather than another isolated catalog "
        "pose. Keep the identifying artwork visible and never obscure it with props.",
    ),
    ListingShot(
        "craft_detail",
        "工艺特写",
        "近距离呈现印花与材质",
        "Macro craft detail: crop tightly into a distinctive visible print, lettering or "
        "surface finish. The detail fills at least 80% of the frame; the complete product "
        "must NOT fit in this image. Preserve the exact visible strokes, spelling and ink "
        "placement. This is a close crop, not the same full-product shot on another table.",
    ),
    ListingShot(
        "scale",
        "比例展示",
        "自然互动，呈现相对大小",
        "Human-scale interaction: use a natural hand holding, touching or using the product "
        "when appropriate, or a realistic environmental scale cue for larger products. "
        "Focus on the interaction and product together with a simple background; preserve "
        "the visible design. Do not add rulers, dimension text or unsupported size claims.",
    ),
    ListingShot(
        "structure",
        "结构细节",
        "展示已知部位与做工",
        "Construction detail: tightly frame a different visible structural area than the "
        "craft macro: an existing handle, rim, seam, edge, joint or closure. Show only a "
        "portion of the product. If the references do not reveal a mechanism, keep it "
        "closed and crop a known edge instead. Never invent an interior or unseen backside.",
    ),
    ListingShot(
        "arrangement",
        "搭配陈列",
        "留白陈列，展示搭配关系",
        "Styled arrangement: place the same single product off-center with deliberate "
        "negative space and a few clearly contextual, non-sale props. Change the spatial "
        "arrangement from the lifestyle shot. No hands, duplicate products, invented "
        "bundles, packaging or implied included accessories.",
    ),
    ListingShot(
        "overview",
        "俯拍构图",
        "换一个构图观察已知外观",
        "Graphic overview: create a top-down arrangement only when the references reveal "
        "the necessary surfaces. Otherwise use the known product view in a diagonal "
        "composition on a flat two-tone backdrop. Keep the full product, with 50-65% "
        "frame occupancy. Do not open, disassemble or imagine unseen surfaces.",
    ),
    ListingShot(
        "editorial",
        "氛围海报",
        "留白与光影，补充品牌氛围",
        "Editorial closing image: product low in the frame with generous clean negative "
        "space above and directional light and shadow on a minimal backdrop. Product "
        "occupies 30-45% of the frame. No tabletop lifestyle set, no typography or badges.",
    ),
)

PLATFORM_DIRECTION = {
    "amazon": "Amazon listing: for the HERO ONLY, pure RGB(255,255,255) white background, "
    "product only. Secondary shots may use context or close crops as assigned.",
    "etsy": "Etsy audience: tactile, authentic materials and natural color. Warm daylight "
    "is suitable for the lifestyle shot, but do not apply one rustic tabletop scene to every shot.",
    "shopify": "Shopify storefront: restrained brand palette and polished photography; "
    "use the different shot roles, not repeated editorial hero variations.",
    "taobao": "Taobao/Tmall listing: mobile-readable product presentation and clear details.",
    "jd": "JD listing: accurate materials, realistic proportions and clean commercial photography.",
    "douyin": "Douyin listing: natural contemporary use context in the lifestyle shot "
    "and immediately legible product presentation throughout the set.",
}


def listing_plan(count: int) -> tuple[ListingShot, ...]:
    if type(count) is not int or not 1 <= count <= len(LISTING_SHOTS):
        raise ValueError("电商套图每次支持 1–8 张")
    return LISTING_SHOTS[:count]


def public_listing_plan() -> dict:
    return {
        "version": PLAN_VERSION,
        "shots": [
            {"code": shot.code, "label": shot.label, "description": shot.description}
            for shot in LISTING_SHOTS
        ],
    }


def listing_prompt(
    *,
    index: int,
    count: int,
    platform: str,
    brief: str,
    reference_count: int,
    has_generated_anchor: bool,
) -> str:
    plan = listing_plan(count)
    shot = plan[index]
    storyboard = "\n".join(
        f"{i + 1}. [{item.code}] {item.direction}" for i, item in enumerate(plan)
    )
    identity = (
        f"Inputs 1-{reference_count} are ORIGINAL PRODUCT REFERENCES of the SAME item. "
        "They are the sole source of truth for product identity; the first is the primary "
        "product and the rest only supplement visible details. Ignore their photographic "
        "backgrounds, clutter and lighting. No generated scene is an identity reference. "
        if reference_count
        else "The input is the generated catalog hero. Use ONLY its product identity, "
        "not its background, camera framing or composition. "
        if has_generated_anchor
        else "Establish one specific product from the brief in this clean catalog hero; "
        "it will be the identity reference for the remaining set. "
    )
    return (
        "Create ONE standalone image in a complementary ecommerce LISTING SET, not a set "
        "of similar hero variations. Each image must communicate a different buying reason "
        "with visibly different framing, scale and context. Keep a coherent color treatment "
        "across the set without repeating a background or camera distance.\n"
        f"FULL SET STORYBOARD (context only; never render a collage):\n{storyboard}\n"
        f"PLATFORM GUIDANCE: {PLATFORM_DIRECTION[platform]}\n"
        f"PRODUCT IDENTITY: {identity}"
        "Preserve exact silhouette, proportions, component count and placement, colors, "
        "materials, print layout, logos and every existing letter/character. Do not mirror "
        "artwork, rewrite text, redesign, recolor, add features or invent unseen surfaces. "
        "Where a new view would require guessing, retain the known view and use the assigned "
        "crop/context instead. Identity preservation takes priority over shot styling.\n"
        f"USER BRIEF (subject and scene preferences within those constraints): {brief}\n"
        f"CURRENT SHOT {index + 1}/{count} [{shot.code}] — {shot.label}: {shot.direction}\n"
        "Render ONLY this assigned shot. Other storyboard entries are separate deliverables. "
        "No collage, contact sheet, split panels, border, added watermark, marketing text, "
        "unsupported claims or badges. Existing product artwork and lettering must remain. "
        "Fill the requested canvas; do not return a different aspect ratio."
    )
