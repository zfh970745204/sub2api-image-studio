from __future__ import annotations

import json
import os
import uuid
from urllib.parse import urlparse

import boto3
import pytest
from botocore.config import Config
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration


def required_test_setting(name: str) -> str:
    if os.getenv("APP_ENV") != "test":
        pytest.skip("external integration tests require APP_ENV=test")
    value = os.getenv(name, "").strip()
    if not value:
        pytest.skip(f"{name} is not configured")
    return value


@pytest.mark.asyncio
async def test_postgres_transaction_uses_ephemeral_test_runs_table() -> None:
    database_url = required_test_setting("TEST_DATABASE_URL")
    database_name = make_url(database_url).database or ""
    assert database_name.endswith("_test"), "integration tests refuse non-test databases"
    engine = create_async_engine(database_url)
    run_id = uuid.uuid4()
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text(
                    """
                    CREATE TEMP TABLE test_runs (
                        id uuid PRIMARY KEY,
                        git_sha varchar NOT NULL,
                        suite varchar NOT NULL,
                        environment varchar NOT NULL,
                        status varchar NOT NULL,
                        started_at timestamptz NOT NULL DEFAULT now(),
                        completed_at timestamptz NULL,
                        summary jsonb NOT NULL
                    ) ON COMMIT PRESERVE ROWS
                    """
                )
            )
            await connection.commit()
            transaction = await connection.begin()
            await connection.execute(
                text(
                    """
                    INSERT INTO test_runs (id, git_sha, suite, environment, status, summary)
                    VALUES (:id, :git_sha, :suite, :environment, :status, CAST(:summary AS jsonb))
                    """
                ),
                {
                    "id": run_id,
                    "git_sha": "integration-test",
                    "suite": "postgres",
                    "environment": "ci",
                    "status": "running",
                    "summary": json.dumps({"isolated": True}),
                },
            )
            await transaction.rollback()
            count = await connection.scalar(text("SELECT count(*) FROM test_runs"))
            assert count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_redis_queue_round_trip_is_namespaced_and_disposable() -> None:
    redis_url = required_test_setting("TEST_REDIS_URL")
    parsed = urlparse(redis_url)
    assert parsed.path not in {"", "/", "/0"}, "integration tests refuse Redis database 0"
    client = Redis.from_url(redis_url, decode_responses=True)
    queue_name = f"sub2image:test:{uuid.uuid4().hex}"
    try:
        await client.rpush(queue_name, "job-1")
        assert await client.lpop(queue_name) == "job-1"
        assert await client.llen(queue_name) == 0
    finally:
        await client.delete(queue_name)
        await client.aclose()


def test_s3_compatible_storage_round_trip_uses_test_bucket() -> None:
    endpoint = required_test_setting("TEST_S3_ENDPOINT")
    access_key = required_test_setting("TEST_S3_ACCESS_KEY")
    secret_key = required_test_setting("TEST_S3_SECRET_KEY")
    bucket = required_test_setting("TEST_S3_BUCKET")
    assert bucket.startswith("sub2image-test-"), "integration tests refuse non-test buckets"
    parsed = urlparse(endpoint)
    assert parsed.hostname in {"127.0.0.1", "localhost", "minio"}
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    object_key = f"quality/{uuid.uuid4().hex}.txt"
    body = b"disposable integration object"
    try:
        client.create_bucket(Bucket=bucket)
        client.put_object(Bucket=bucket, Key=object_key, Body=body)
        assert client.get_object(Bucket=bucket, Key=object_key)["Body"].read() == body
    finally:
        client.delete_object(Bucket=bucket, Key=object_key)
        client.delete_bucket(Bucket=bucket)
