import asyncio
import base64
import hashlib
import sys
import uuid
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update
from test_assets import asset_context as asset_fixture
from test_assets import client_for, login, raster_bytes, seed_user, upload

from app.config import Settings
from app.repositories.models import (
    Asset,
    ImageJob,
    OperationCatalog,
    PointAccount,
    PointTransaction,
)
from app.services import background_models, cutout_process
from app.services.cutout_process import BackgroundRemovalRunner
from app.services.image_executor import ImageJobExecutor
from app.workers.worker import execute_image_job

asset_context = asset_fixture


@pytest.fixture
def model_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("U2NET_HOME", raising=False)
    return Settings().model_copy(
        update={
            "background_model_dir": tmp_path / "cache",
            "background_model_bundle_dir": tmp_path / "bundle",
        }
    )


def test_empty_or_corrupt_existing_volume_uses_verified_image_bundle(model_settings, monkeypatch):
    weights = b"known model bytes"
    monkeypatch.setitem(
        background_models.MODEL_CHECKSUMS, "u2net", hashlib.md5(weights).hexdigest()
    )
    bundle = model_settings.background_model_bundle_dir / "u2net.onnx"
    bundle.parent.mkdir()
    bundle.write_bytes(weights)
    corrupt = model_settings.background_model_dir / "models" / "u2net" / "u2net.onnx"
    corrupt.parent.mkdir(parents=True)
    for raw in (b"", b"partial download"):
        corrupt.write_bytes(raw)
        assert background_models.resolve_background_model(model_settings, "u2net") == bundle
        assert corrupt.read_bytes() == raw  # Preserve mounted files.
    corrupt.write_bytes(weights)
    assert background_models.resolve_background_model(model_settings, "u2net") == corrupt


def test_missing_model_fails_offline_and_manual_cache_override_is_honored(
    model_settings, monkeypatch, tmp_path
):
    def no_download(*args, **kwargs):
        raise AssertionError("Runtime must not attempt a model download")

    monkeypatch.setattr(background_models.urllib.request, "urlopen", no_download)
    with pytest.raises(RuntimeError, match="模型未就绪"):
        background_models.resolve_background_model(model_settings, "u2net")
    with pytest.raises(RuntimeError, match="名称无效"):
        background_models.resolve_background_model(model_settings, "../u2net")
    weights = b"known model bytes"
    monkeypatch.setitem(
        background_models.MODEL_CHECKSUMS, "u2net", hashlib.md5(weights).hexdigest()
    )
    override = tmp_path / "override"
    override.mkdir()
    (override / "u2net.onnx").write_bytes(weights)
    monkeypatch.setenv("U2NET_HOME", str(override))
    assert background_models.resolve_background_model(model_settings, "u2net") == (
        override / "u2net.onnx"
    )


def test_provisioning_verifies_download_and_does_not_install_partial_model(tmp_path, monkeypatch):
    weights = b"known model bytes"
    monkeypatch.setitem(
        background_models.MODEL_CHECKSUMS, "u2net", hashlib.md5(weights).hexdigest()
    )
    destination = tmp_path / "u2net.onnx"
    destination.write_bytes(b"existing incomplete model")
    monkeypatch.setattr(
        background_models.urllib.request, "urlopen", lambda *args, **kwargs: BytesIO(b"bad data")
    )
    with pytest.raises(RuntimeError, match="checksum"):
        background_models.download_background_model(tmp_path, "u2net")
    assert destination.read_bytes() == b"existing incomplete model"
    assert list(tmp_path.glob("*.partial")) == []
    monkeypatch.setattr(
        background_models.urllib.request, "urlopen", lambda *args, **kwargs: BytesIO(weights)
    )
    assert background_models.download_background_model(tmp_path, "u2net") == destination
    assert destination.read_bytes() == weights

    def no_repeat(*args, **kwargs):
        raise AssertionError("Valid installed model should be reused")

    monkeypatch.setattr(background_models.urllib.request, "urlopen", no_repeat)
    assert background_models.download_background_model(tmp_path, "u2net") == destination


@pytest.fixture
def local_runner(model_settings, monkeypatch):
    monkeypatch.setattr(cutout_process, "configure_image_runtime", lambda settings: None)
    monkeypatch.setattr(
        cutout_process, "resolve_background_model", lambda settings, model: Path("local.onnx")
    )
    return BackgroundRemovalRunner(model_settings)


@pytest.mark.asyncio
async def test_timeout_kills_child_before_next_cutout_and_waiters_are_cancellable(
    local_runner, monkeypatch
):
    original = asyncio.create_subprocess_exec
    launched = asyncio.Event()
    processes = []

    async def launch(*args, **kwargs):
        # Exercise actual OS process cancellation without needing an ONNX model.
        script = (
            "import sys,time; sys.stdin.buffer.read(); time.sleep(300)"
            if not processes
            else "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b'\\x89PNG\\r\\n\\x1a\\nresult')"
        )
        process = await original(sys.executable, "-c", script, **kwargs)
        processes.append(process)
        launched.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    deadline = asyncio.timeout(None)

    async def first_job():
        async with deadline:
            return await local_runner.remove(b"image")

    first = asyncio.create_task(first_job())
    try:
        await asyncio.wait_for(launched.wait(), 5)
        waiting = asyncio.create_task(local_runner.remove(b"waiting-image"))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert len(processes) == 1
        deadline.reschedule(asyncio.get_running_loop().time())
        with pytest.raises(TimeoutError):
            await first
        assert processes[0].returncode is not None
        assert await local_runner.remove(b"next-image") == b"\x89PNG\r\n\x1a\nresult"
        assert len(processes) == 2 and processes[1].returncode == 0
    finally:
        if not first.done():
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first


@pytest.mark.asyncio
async def test_crashed_child_releases_slot_and_reports_engine_failure(local_runner, monkeypatch):
    original = asyncio.create_subprocess_exec

    async def crash(*args, **kwargs):
        return await original(sys.executable, "-c", "raise SystemExit(7)", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", crash)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="抠图引擎执行失败"):
            await asyncio.wait_for(local_runner.remove(b"image"), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["succeeded", "timed_out", "failed"])
async def test_cutout_process_job_publishes_or_refunds_once(
    asset_context, local_runner, monkeypatch, outcome
):
    owner = await seed_user(asset_context, email="cutout-owner@example.test")
    async with asset_context.database.session_factory() as session:
        await session.execute(
            update(OperationCatalog)
            .where(OperationCatalog.code == "cutout.smart")
            .values(timeout_seconds=1 if outcome == "timed_out" else 30)
        )
        await session.commit()
    async with client_for(asset_context, "cutout-owner") as client:
        await login(client, owner.email)
        asset = (await upload(client, raster_bytes())).json()["asset"]
        quoted = await client.post(
            "/api/v1/jobs/quote",
            json={"operation_code": "cutout.smart", "source_asset_id": asset["id"]},
        )
        assert quoted.status_code == 201, quoted.text
        quote = quoted.json()["quote"]
        created = await client.post(
            "/api/v1/jobs",
            headers={"Idempotency-Key": "cutout-process-job"},
            json={"quote_id": quote["id"]},
        )
        assert created.status_code == 201, created.text
        job_id = created.json()["job"]["id"]

    original = asyncio.create_subprocess_exec
    processes = []
    output = raster_bytes(mode="RGBA")

    async def launch(*args, **kwargs):
        script = {
            "timed_out": "import sys,time; sys.stdin.buffer.read(); time.sleep(300)",
            "failed": "raise SystemExit(7)",
            "succeeded": f"import sys,base64; sys.stdin.buffer.read(); sys.stdout.buffer.write(base64.b64decode({base64.b64encode(output)!r}))",
        }[outcome]
        process = await original(sys.executable, "-c", script, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    executor = ImageJobExecutor(
        asset_context.settings, asset_context.database, asset_context.storage
    )
    executor.background_remover = local_runner
    ctx = {
        "runtime": SimpleNamespace(database=asset_context.database, instance_name="cutout-test"),
        "image_job_executor": executor,
    }
    result = await execute_image_job(ctx, job_id)
    assert result["status"] == outcome
    assert len(processes) == 1 and processes[0].returncode is not None
    assert (await execute_image_job(ctx, job_id))["status"] == "ignored"
    assert len(processes) == 1
    async with asset_context.database.session_factory() as session:
        job = await session.get(ImageJob, uuid.UUID(job_id))
        balance = await session.scalar(
            select(PointAccount.balance).where(PointAccount.user_id == owner.id)
        )
        refunds = await session.scalar(
            select(func.count(PointTransaction.id)).where(
                PointTransaction.user_id == owner.id, PointTransaction.entry_type == "refund"
            )
        )
        if outcome == "succeeded":
            result_asset = await session.get(Asset, job.output_asset_id)
            assert result_asset.status == "ready" and result_asset.has_alpha
            assert balance == 200 - quote["final_points"] and refunds == 0
        else:
            assert job.output_asset_id is None and job.refund_status == "refunded"
            assert balance == 200 and refunds == 1
            assert job.error_code == (
                "JOB_TIMED_OUT" if outcome == "timed_out" else "IMAGE_ENGINE_UNAVAILABLE"
            )
