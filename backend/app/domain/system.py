from enum import StrEnum


class ServiceType(StrEnum):
    WEB = "web"
    WORKER = "worker"
    SCHEDULER = "scheduler"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"
