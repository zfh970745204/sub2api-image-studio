import os
import time

from app.storage import ResultStore


def test_store_rejects_path_traversal(tmp_path) -> None:
    store = ResultStore(tmp_path, ttl_hours=1)
    filename, path = store.save(b"image", "png")
    assert store.resolve(filename) == path
    assert store.resolve("../secret.png") is None


def test_store_cleans_expired_results(tmp_path) -> None:
    store = ResultStore(tmp_path, ttl_hours=1)
    _, path = store.save(b"old", "png")
    old = time.time() - 7200
    os.utime(path, (old, old))
    assert store.cleanup() == 1
    assert not path.exists()
