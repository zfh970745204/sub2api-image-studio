"""Bound CPU/memory concurrency and terminate inference when its job is cancelled."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
import time
from pathlib import Path

from app.config import Settings, get_settings
from app.services.background_models import resolve_background_model
from app.services.image_runtime import configure_image_runtime

logger = logging.getLogger(__name__)


class BackgroundRemovalRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # The default 1.25 GiB Worker cannot hold two U2Net inferences safely.
        # Waiting here is cancellable and never starts another native process.
        self._slot = asyncio.Lock()

    async def warmup(self) -> None:
        try:
            async with asyncio.timeout(self.settings.background_model_startup_timeout_seconds):
                await self._run(None)
        except TimeoutError as exc:
            raise RuntimeError(
                "抠图模型启动验证超时，请管理员检查 Worker 资源和模型文件。"
            ) from exc

    async def remove(self, source: bytes) -> bytes:
        return await self._run(source)

    async def _run(self, source: bytes | None) -> bytes:
        async with self._slot:
            # Missing models fail before launching Python or importing native
            # libraries, with no implicit network access or download retry.
            await asyncio.to_thread(configure_image_runtime, self.settings)
            await asyncio.to_thread(
                resolve_background_model, self.settings, self.settings.background_model
            )
            env = dict(os.environ)
            env.update(
                BACKGROUND_MODEL=self.settings.background_model,
                BACKGROUND_MODEL_DIR=str(self.settings.background_model_dir),
                BACKGROUND_MODEL_BUNDLE_DIR=str(self.settings.background_model_bundle_dir),
                BACKGROUND_MODEL_THREADS=str(self.settings.background_model_threads),
                OMP_NUM_THREADS=str(self.settings.background_model_threads),
                OPENBLAS_NUM_THREADS="1",
                MKL_NUM_THREADS="1",
                NUMBA_NUM_THREADS=str(self.settings.background_model_threads),
            )
            env["PYTHONPATH"] = os.pathsep.join(
                filter(None, (str(Path(__file__).resolve().parents[2]), env.get("PYTHONPATH")))
            )
            started = time.perf_counter()
            logger.info("cutout process starting", extra={"operation": "cutout.smart"})
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.services.cutout_process",
                "--probe" if source is None else "--stdin",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            communication = asyncio.create_task(process.communicate(source))
            try:
                # Shield pipe draining, not inference. Cancellation kills and
                # reaps the child before releasing its resource slot.
                output, stderr = await asyncio.shield(communication)
            except BaseException:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                await asyncio.shield(communication)
                logger.warning("cutout process terminated", extra={"operation": "cutout.smart"})
                raise
            if process.returncode != 0 or not output.startswith(b"\x89PNG\r\n\x1a\n"):
                logger.error(
                    "cutout process failed: exit=%s stderr=%s",
                    process.returncode,
                    stderr.decode("utf-8", errors="replace")[-4000:],
                    extra={"operation": "cutout.smart"},
                )
                raise RuntimeError("抠图引擎执行失败，请管理员检查模型及 Worker 内存配置。")
            logger.info(
                "cutout process finished in %.2fs (threads=%s, probe=%s)",
                time.perf_counter() - started,
                self.settings.background_model_threads,
                source is None,
                extra={"operation": "cutout.smart"},
            )
            return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--probe", action="store_true")
    mode.add_argument("--stdin", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    with contextlib.redirect_stdout(sys.stderr):
        from io import BytesIO

        from PIL import Image, ImageDraw

        from app.image_ops import remove_background

        if args.probe:
            image = Image.new("RGB", (64, 64), "white")
            ImageDraw.Draw(image).ellipse((12, 8, 52, 60), fill="black")
            buffer = BytesIO()
            image.save(buffer, "PNG")
            source = buffer.getvalue()
        else:
            source = sys.stdin.buffer.read()
        result = remove_background(source, settings.background_model, settings=settings)
    sys.stdout.buffer.write(result)


if __name__ == "__main__":
    main()
