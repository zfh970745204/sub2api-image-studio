import re
import time
import uuid
from pathlib import Path

SAFE_RESULT_NAME = re.compile(r"^[0-9a-f]{32}\.(png|jpe?g|webp|svg)$")


class ResultStore:
    def __init__(self, root: Path, ttl_hours: int) -> None:
        self.root = root.resolve()
        self.ttl_seconds = ttl_hours * 60 * 60
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, data: bytes, extension: str) -> tuple[str, Path]:
        normalized_extension = "jpg" if extension == "jpeg" else extension
        filename = f"{uuid.uuid4().hex}.{normalized_extension}"
        path = self.root / filename
        path.write_bytes(data)
        return filename, path

    def resolve(self, filename: str) -> Path | None:
        if not SAFE_RESULT_NAME.fullmatch(filename):
            return None
        path = (self.root / filename).resolve()
        if path.parent != self.root or not path.is_file():
            return None
        return path

    def cleanup(self) -> int:
        threshold = time.time() - self.ttl_seconds
        removed = 0
        for path in self.root.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < threshold:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
