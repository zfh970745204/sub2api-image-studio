from __future__ import annotations

POINT_ENTRY_TYPES = frozenset(
    {"grant", "consume", "refund", "adjust", "renewal", "promotion", "reversal"}
)
POINT_ACCOUNT_STATUSES = frozenset({"active", "frozen"})
POINT_ADJUSTMENT_STATUSES = frozenset({"pending", "approved", "rejected", "applied"})
