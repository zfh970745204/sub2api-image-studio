import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ComparisonPreview } from "./ComparisonPreview";
import type { Asset } from "./user-api";

const source = { asset: { id: "source", width: 1200, height: 800 } as Asset, url: "/source.png" };
const result = { asset: { id: "result", width: 2400, height: 1600, has_alpha: true } as Asset, url: "/result.png" };
const props = { source, result, compare: true, backgroundClass: "preview-transparent", empty: "上传图片", onError: vi.fn() };

describe("source and result comparison", () => {
  it("keeps both images visible and moves their magnification to the same normalized position", () => {
    render(<ComparisonPreview {...props} />);
    const original = screen.getByRole("img", { name: "来源素材预览" });
    const processed = screen.getByRole("img", { name: "图片任务结果" });
    const stage = screen.getByRole("group", { name: /^原图细节/ });
    fireEvent.focus(stage);
    fireEvent.keyDown(stage, { key: "ArrowRight" });
    expect(parseFloat(original.style.transformOrigin)).toBeCloseTo(55);
    expect(processed.style.transformOrigin).toBe(original.style.transformOrigin);
    expect(processed.style.transform).toBe("scale(3)");
    fireEvent.change(screen.getByLabelText("细节倍率"), { target: { value: "4" } });
    expect(original.style.transform).toBe("scale(4)");
    fireEvent.keyDown(stage, { key: "Escape" });
    expect(processed.style.transform).toBe("");
    expect(original).toBeVisible();
  });
  it("provides a result placeholder before processing and lets the mask receive pointer events", () => {
    render(<ComparisonPreview {...props} result={null} sourceOverlay={<canvas aria-label="涂抹区域" />} />);
    expect(screen.getByRole("article", { name: "原图" })).toBeVisible();
    expect(screen.getByRole("article", { name: "处理结果" })).toBeVisible();
    expect(screen.getByLabelText("涂抹区域")).toBeInTheDocument();
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });
  it("shows only the output for newly generated images", () => {
    render(<ComparisonPreview {...props} compare={false} />);
    expect(screen.queryByRole("img", { name: "来源素材预览" })).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: "图片任务结果" })).toBeVisible();
  });
});
