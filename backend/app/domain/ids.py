from __future__ import annotations

import secrets
import time
import uuid


def uuid7() -> uuid.UUID:
    """Create an RFC 9562 UUIDv7 using the current Unix time in milliseconds."""
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    randomness = secrets.randbits(74)
    rand_a = randomness >> 62
    rand_b = randomness & ((1 << 62) - 1)
    value = (timestamp_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)
