from __future__ import annotations

ASSET_KINDS = frozenset({"original", "result", "mask", "thumbnail", "vector"})
ASSET_STATUSES = frozenset({"uploading", "ready", "quarantined", "deleted"})
ASSET_ACCESS_ACTIONS = frozenset({"preview", "download", "admin_preview"})
DELETION_QUEUE_STATUSES = frozenset({"pending", "completed", "failed", "cancelled"})

OBJECT_KIND_NAMES = {
    "original": "original",
    "result": "result",
    "mask": "mask",
    "thumbnail": "thumb",
    "vector": "vector",
}
