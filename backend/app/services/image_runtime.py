"""Writable model/JIT directories must be ready before importing rembg/Numba."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from threading import RLock

from app.config import Settings, get_settings

_initialization_lock = RLock()


def configure_image_runtime(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    with _initialization_lock:
        for name, default in (
            ("NUMBA_CACHE_DIR", settings.numba_cache_dir),
            ("U2NET_HOME", settings.background_model_dir),
        ):
            directory = Path(os.environ.get(name) or default).resolve()
            try:
                directory.mkdir(parents=True, exist_ok=True)
                # Check the mounted volume as the actual runtime user, not just
                # image-layer ownership or os.access's permission prediction.
                with tempfile.TemporaryFile(dir=directory):
                    pass
            except OSError as exc:
                raise RuntimeError(
                    f"抠图运行目录不可写（{name}），请管理员检查缓存和模型目录权限。"
                ) from exc
            os.environ[name] = str(directory)


def warmup_image_runtime(settings: Settings | None = None) -> None:
    configure_image_runtime(settings)
    try:
        # Importing pymatting compiles its cached functions. No model download or
        # external image request is needed to catch the production locator error.
        import rembg  # noqa: F401
    except (ImportError, RuntimeError, SystemExit) as exc:
        raise RuntimeError("抠图引擎初始化失败，请管理员检查依赖及缓存目录。") from exc
