from __future__ import annotations

_EXPORTS = ("sync_once", "SyncWorker", "SyncConfig", "SyncResult", "Outcome",
            "SyncStarted", "Applying", "SyncFinished", "IconReady", "IconFailed")

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name in _EXPORTS:
        from ul_house.sync import service

        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
