"""Backward-compatible imports for the former single-provider client."""

from .image_gateway import ImageServiceClient as Sub2APIClient
from .image_provider_types import ImageServiceError as Sub2APIError
from .image_provider_types import UpstreamImage

__all__ = ["Sub2APIClient", "Sub2APIError", "UpstreamImage"]
