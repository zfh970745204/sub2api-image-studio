from app.catalog import StudioCatalog


def test_catalog_records_branch_lineage(tmp_path) -> None:
    catalog = StudioCatalog(tmp_path / "studio.db")
    source = catalog.add_asset(
        filename="a.png",
        operation="upload",
        mime_type="image/png",
        width=100,
        height=80,
        size_bytes=3,
        data=b"one",
    )
    job = catalog.create_job("restore", source["id"], {"mode": "logo"})
    result = catalog.add_asset(
        filename="b.png",
        operation="restore",
        mime_type="image/png",
        width=400,
        height=320,
        size_bytes=3,
        data=b"two",
        parent_id=source["id"],
        job_id=job["id"],
    )
    catalog.complete_job(job["id"], result["id"])

    lineage = catalog.lineage(result["id"])
    assert [asset["id"] for asset in lineage] == [source["id"], result["id"]]
    assert result["root_id"] == source["id"]
    assert catalog.get_job(job["id"])["status"] == "completed"
