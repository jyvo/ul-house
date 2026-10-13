from __future__ import annotations

from typing import Literal

RefusedReason = Literal["bad_signature", "revision_lower", "schema_unsupported", "api_unsupported"]


class SyncError(Exception):
    """Base class of every sync failure."""


class ProtocolError(SyncError):
    """Malformed document: bad JSON, wrong type, missing field, bad pattern."""


class VerificationError(SyncError):
    """Bad signature, sha256 mismatch, oversize artifact, no trusted key."""


class Refused(SyncError):
    """The release is refused; local data is kept and the user is told."""

    def __init__(self, reason: RefusedReason, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason: RefusedReason = reason
        self.message = message or reason


class RevisionMismatch(SyncError):
    """package.from_revision != local revision, or to_revision <= local."""


class PackageError(SyncError):
    """Package inconsistent with local state or with the entity registry."""


class ApplyError(SyncError):
    """The apply transaction failed and was rolled back."""


class DatabaseBusy(ApplyError):
    """the write lock is held by another connection (database is locked / busy): a temporary condition"""


class SnapshotError(SyncError):
    """Snapshot invalid, or its transaction was rolled back."""


class IconError(SyncError):
    """Icon pack or icon file invalid."""
