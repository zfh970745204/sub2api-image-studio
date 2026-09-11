"""Caching policy for public build files, independent of private API responses."""

import re
from pathlib import Path

from starlette.staticfiles import StaticFiles

_HASHED_BUILD_FILE = re.compile(r".+-[A-Za-z0-9_-]{8,}\.(?:js|css|woff2?)$")


class FrontendStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code in {200, 304}:
            # StaticFiles normalizes URL paths using the host OS separator.
            asset = Path(path)
            immutable = asset.parent.as_posix() == "assets" and _HASHED_BUILD_FILE.fullmatch(
                asset.name
            )
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable" if immutable else "no-cache"
            )
        return response
