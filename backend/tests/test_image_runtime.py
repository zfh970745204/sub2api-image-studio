import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.services.image_runtime import configure_image_runtime


def test_runtime_checks_custom_mounted_directories_before_import(tmp_path, monkeypatch):
    cache, models = tmp_path / "cache", tmp_path / "models"
    monkeypatch.setenv("NUMBA_CACHE_DIR", str(cache))
    monkeypatch.setenv("U2NET_HOME", str(models))
    configure_image_runtime(Settings())
    assert cache.is_dir() and models.is_dir()
    assert os.environ["U2NET_HOME"] == str(models)
    monkeypatch.setenv("NUMBA_CACHE_DIR", str(tmp_path / "not-a-directory"))
    (tmp_path / "not-a-directory").touch()
    with pytest.raises(RuntimeError, match="NUMBA_CACHE_DIR"):
        configure_image_runtime(Settings())


def test_real_pymatting_import_uses_configured_writable_cache(tmp_path):
    env = dict(os.environ)
    env["NUMBA_CACHE_DIR"] = str(tmp_path / "numba")
    env["U2NET_HOME"] = str(tmp_path / "models")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.services.image_runtime import warmup_image_runtime; warmup_image_runtime(); from pymatting.util.kdtree import _make_tree; import numba; import os; assert numba.config.CACHE_DIR == os.environ['NUMBA_CACHE_DIR']; print('cache-ready')",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cache-ready" in result.stdout
    assert list((tmp_path / "numba").rglob("*.nbc"))
