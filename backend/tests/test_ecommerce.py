import base64
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from test_assets import asset_context as asset_fixture
from test_assets import client_for, login, raster_bytes, seed_user, upload

from app.config import Settings
from app.domain.ecommerce import LISTING_SHOTS, PLAN_VERSION, listing_plan, listing_prompt
from app.domain.jobs import ECOMMERCE_PLATFORMS
from app.repositories.models import Asset, ImageJob, PointAccount
from app.services.image_executor import ImageJobExecutor
from app.services.jobs import ClaimedJob
from app.sub2api import Sub2APIClient, UpstreamImage
from app.workers.worker import execute_image_job

asset_context = asset_fixture


@pytest.mark.parametrize("platform", sorted(ECOMMERCE_PLATFORMS))
def test_set_brief_assigns_distinct_roles_without_changing_product(platform):
    prompts = [
        listing_prompt(
            index=i,
            count=5,
            platform=platform,
            brief="保留杯身文字",
            reference_count=2,
            has_generated_anchor=False,
        )
        for i in range(5)
    ]
    assert len(set(prompts)) == 5
    for index, prompt in enumerate(prompts):
        current = prompt.split("CURRENT SHOT ")[1]
        assert current.startswith(f"{index + 1}/5 [{LISTING_SHOTS[index].code}]")
        assert "ORIGINAL PRODUCT REFERENCES" in prompt
        assert "sole source of truth" in prompt
        assert "every existing letter/character" in prompt
        assert "保留杯身文字" in prompt
        assert "FULL SET STORYBOARD" in prompt
    assert "complete product must NOT fit" in prompts[2]
    assert "No room, tabletop styling" in prompts[0]
    assert "different visible structural area" in prompts[4]
    if platform == "amazon":
        assert "for the HERO ONLY" in prompts[1]


@pytest.mark.parametrize("count", range(1, 9))
@pytest.mark.asyncio
async def test_every_shot_uses_originals_without_generated_scene_feedback(count):
    settings = Settings(
        sub2api_api_key="test-key", sub2api_base_url="https://upstream.example.test/v1"
    )
    client = SimpleNamespace(
        edit=AsyncMock(
            side_effect=[UpstreamImage(f"output-{i}".encode(), "png", None) for i in range(count)]
        ),
        generate=AsyncMock(),
    )
    executor = ImageJobExecutor(settings, None, None, sub2api=client)
    originals = [b"original-front", b"original-detail"]
    executor._references = AsyncMock(return_value=originals)
    claim = ClaimedJob(
        uuid.uuid4(),
        uuid.uuid4(),
        "ai.ecommerce",
        None,
        {"image_count": count, "platform": "etsy", "size": "1024x1536"},
        1,
        2400,
        30,
    )
    outputs = [output async for output in executor._outputs(claim, None)]
    assert len(outputs) == count
    assert client.edit.await_count == count
    client.generate.assert_not_awaited()
    for index, call in enumerate(client.edit.call_args_list):
        assert call.kwargs["image_png"] == originals[0]
        assert call.kwargs["reference_images"] == originals[1:]
        assert call.kwargs["size"] == "1024x1536"
        metadata = outputs[index][3]
        assert metadata["shot_role"] == listing_plan(count)[index].code
        assert metadata["shot_plan_version"] == PLAN_VERSION
        assert metadata["identity_anchor"] == "original-references"


@pytest.mark.asyncio
async def test_text_only_legacy_task_anchors_every_later_shot_to_the_same_clean_hero():
    settings = Settings(
        sub2api_api_key="test-key", sub2api_base_url="https://upstream.example.test/v1"
    )
    client = SimpleNamespace(
        generate=AsyncMock(return_value=UpstreamImage(b"hero", "png", None)),
        edit=AsyncMock(return_value=UpstreamImage(b"secondary", "png", None)),
    )
    executor = ImageJobExecutor(settings, None, None, sub2api=client)
    claim = ClaimedJob(
        uuid.uuid4(),
        uuid.uuid4(),
        "ai.ecommerce",
        None,
        {"prompt": "a ceramic mug", "image_count": 3},
        1,
        2400,
        30,
    )
    outputs = [output async for output in executor._outputs(claim, None)]
    assert len(outputs) == 3
    client.generate.assert_awaited_once()
    assert client.edit.await_count == 2
    for call in client.edit.call_args_list:
        assert call.kwargs["image_png"] == b"hero"
        assert call.kwargs["reference_images"] == []
        assert "Use ONLY its product identity" in call.kwargs["prompt"]


@pytest.mark.parametrize("fail_second", [False, True])
@pytest.mark.asyncio
async def test_set_roles_persist_and_partial_failure_publishes_nothing(asset_context, fail_second):
    owner = await seed_user(asset_context, email="listing-set@example.test")
    async with client_for(asset_context, "listing-set") as client:
        await login(client, owner.email)
        source = (await upload(client, raster_bytes())).json()["asset"]
        operations = (await client.get("/api/v1/operations")).json()["items"]
        plan = next(item for item in operations if item["code"] == "ai.ecommerce")["ecommerce_plan"]
        assert plan["version"] == PLAN_VERSION
        assert len(plan["shots"]) == 8
        assert all("direction" not in shot for shot in plan["shots"])
        parameters = {"platform": "etsy", "image_count": 3, "reference_asset_ids": [source["id"]]}
        quoted = await client.post(
            "/api/v1/jobs/quote",
            json={
                "operation_code": "ai.ecommerce",
                "source_asset_id": source["id"],
                "parameters": parameters,
            },
        )
        assert quoted.status_code == 201, quoted.text
        quote = quoted.json()["quote"]
        submitted = await client.post(
            "/api/v1/jobs",
            headers={"Idempotency-Key": "listing-set"},
            json={"quote_id": quote["id"], "parameters": parameters},
        )
        assert submitted.status_code == 201, submitted.text
        job_id = submitted.json()["job"]["id"]

    calls = []
    raw = raster_bytes()

    def upstream(request):
        calls.append(request.content)
        assert request.url.path == "/v1/images/edits"
        if fail_second and len(calls) == 2:
            return httpx.Response(400, json={"error": {"message": "invalid image"}})
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(raw).decode()}]})

    settings = asset_context.settings.model_copy(
        update={
            "sub2api_api_key": "test-key",
            "sub2api_base_url": "https://upstream.example.test/v1",
        }
    )
    executor = ImageJobExecutor(
        settings,
        asset_context.database,
        asset_context.storage,
        sub2api=Sub2APIClient(settings, transport=httpx.MockTransport(upstream)),
    )
    context = {
        "runtime": SimpleNamespace(
            database=asset_context.database,
            object_storage=asset_context.storage,
            instance_name="listing-set-test",
        ),
        "image_job_executor": executor,
    }
    await execute_image_job(context, job_id)
    async with asset_context.database.session_factory() as session:
        task = await session.get(ImageJob, uuid.UUID(job_id))
        assert len(calls) == (2 if fail_second else 3)
        assert all(b"image[]" not in call for call in calls)  # No appended generated scene.
        balance = await session.scalar(
            select(PointAccount.balance).where(PointAccount.user_id == owner.id)
        )
        generated = list(await session.scalars(select(Asset).where(Asset.source_job_id == task.id)))
        if fail_second:
            assert task.status == "failed" and task.refund_status == "refunded"
            assert not task.output_asset_ids and not task.output_asset_id
            assert balance == 200
            assert all(asset.status != "ready" for asset in generated)
        else:
            assert task.status == "succeeded"
            assert len(task.output_asset_ids) == 3
            assert balance == 200 - quote["final_points"]
            for index, asset_id in enumerate(task.output_asset_ids):
                asset = await session.get(Asset, uuid.UUID(str(asset_id)))
                assert asset.status == "ready"
                assert asset.parent_asset_id == uuid.UUID(source["id"])
                assert asset.asset_metadata["shot_label"] == plan["shots"][index]["label"]
    await execute_image_job(context, job_id)
    assert len(calls) == (2 if fail_second else 3)  # Queue replay never regenerates the set.
