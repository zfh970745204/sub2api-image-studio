"""Resolve local weights without downloading inside a paid image task."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import tempfile
import time
import urllib.request
from pathlib import Path

from app.config import Settings

# Pinned checksums published with the rembg models. Downloads are an explicit
# provisioning step; the runtime never invokes rembg's automatic downloader.
MODEL_CHECKSUMS = {
    "u2net": "60024c5c889badc19c04ad937298a77b",
    "u2netp": "8e83ca70e441ab06c318d82300c84806",
}


def _valid_model(path: Path, model: str) -> bool:
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        expected = MODEL_CHECKSUMS.get(model)
        if expected is None:
            return True  # Other locally provisioned models are checked by ONNX at load.
        with path.open("rb") as source:
            return hashlib.file_digest(source, "md5").hexdigest() == expected
    except OSError:
        return False


def resolve_background_model(settings: Settings, model: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", model):
        raise RuntimeError("抠图模型名称无效，请管理员检查模型配置。")
    directory = Path(os.environ.get("U2NET_HOME") or settings.background_model_dir)
    # The bundle lives outside /models so an old/empty Docker volume cannot
    # obscure weights included in the new image. Existing caches remain intact.
    for path in (
        directory / "models" / model / f"{model}.onnx",
        directory / f"{model}.onnx",
        settings.background_model_bundle_dir / f"{model}.onnx",
    ):
        if _valid_model(path, model):
            return path.resolve()
    raise RuntimeError("抠图模型未就绪或校验失败，请管理员更新含模型的镜像或预先安装模型。")


def download_background_model(directory: Path, model: str) -> Path:
    """Explicit build/setup command, with bounded reads and an atomic install."""
    if model not in MODEL_CHECKSUMS:
        raise ValueError("Automatic provisioning supports u2net and u2netp only")
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{model}.onnx"
    if _valid_model(destination, model):
        return destination
    temporary: Path | None = None
    try:
        url = f"https://github.com/danielgatis/rembg/releases/download/v0.0.0/{model}.onnx"
        deadline = time.monotonic() + 300
        with (
            urllib.request.urlopen(url, timeout=30) as response,
            tempfile.NamedTemporaryFile(dir=directory, suffix=".partial", delete=False) as target,
        ):
            temporary = Path(target.name)
            while chunk := response.read(1024 * 1024):
                if time.monotonic() > deadline:
                    raise TimeoutError("Model provisioning exceeded five minutes")
                target.write(chunk)
        if not _valid_model(temporary, model):
            raise RuntimeError("Downloaded model checksum does not match the pinned release")
        temporary.replace(destination)
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=tuple(MODEL_CHECKSUMS), default="u2net")
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    print(download_background_model(args.directory, args.model))


if __name__ == "__main__":
    main()
