from __future__ import annotations

import re
from typing import Any


def print_options(parameters: dict[str, Any]) -> tuple[str, str | None]:
    mode = parameters.get("output_mode", "transparent")
    color = parameters.get("background_color")
    if not isinstance(mode, str) or mode not in {"transparent", "opaque"}:
        raise ValueError("印花输出模式必须为透明或原产品底色")
    if color is not None and (
        not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color)
    ):
        raise ValueError("产品底色必须为有效的六位颜色值")
    return mode, color.upper() if color else None
