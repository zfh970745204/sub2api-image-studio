from __future__ import annotations

import re
from typing import Any

PRINT_OUTPUT_SIZES = {
    "2048x2048": "1024x1024",
    "2048x3072": "1024x1536",
    "3072x2048": "1536x1024",
    "3072x3072": "1024x1024",
}


def print_output_size(parameters: dict[str, Any]) -> str | None:
    size = parameters.get("output_size")
    if size is not None and (not isinstance(size, str) or size not in PRINT_OUTPUT_SIZES):
        raise ValueError("请选择支持的印花输出尺寸")
    return size


def print_options(parameters: dict[str, Any]) -> tuple[str, str | None]:
    print_output_size(parameters)
    mode = parameters.get("output_mode", "transparent")
    color = parameters.get("background_color")
    if not isinstance(mode, str) or mode not in {"transparent", "opaque"}:
        raise ValueError("印花输出模式必须为透明或原产品底色")
    if color is not None and (
        not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color)
    ):
        raise ValueError("产品底色必须为有效的六位颜色值")
    return mode, color.upper() if color else None
