"""
Atomic JSON persistence for LIFO grid runners.

One file per runner, written via tmp + os.replace so a crash mid-save cannot
corrupt the file. Loads are forgiving — a corrupt or missing file yields None
and the runner starts fresh.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


def _default_dir() -> Path:
    return Path(os.getenv("LIFO_STATE_DIR", ".")).resolve()


def state_path(venue_name: str) -> Path:
    """Map venue name → state file (e.g. 'binance-live' → state_lifo_binance_live.json)."""
    safe = venue_name.replace("-", "_")
    return _default_dir() / f"state_lifo_{safe}.json"


class LifoStateStore:
    """Per-runner, async-safe JSON store."""

    def __init__(self, venue_name: str) -> None:
        self.path = state_path(venue_name)
        self._lock = asyncio.Lock()

    async def load(self) -> Optional[dict[str, Any]]:
        try:
            def _read() -> Optional[dict[str, Any]]:
                if not self.path.exists():
                    return None
                with open(self.path, "r", encoding="utf-8") as fh:
                    return json.load(fh)

            return await asyncio.to_thread(_read)
        except json.JSONDecodeError as exc:
            log.warning("Corrupt state file %s: %s — starting fresh", self.path, exc)
            backup = self.path.with_suffix(".corrupt.json")
            try:
                self.path.replace(backup)
                log.warning("Backed up corrupt state to %s", backup)
            except Exception:
                pass
            return None
        except Exception as exc:
            log.warning("Could not load state %s: %s", self.path, exc)
            return None

    async def save(self, data: dict[str, Any]) -> None:
        """Atomic write: tmpfile + os.replace."""
        async with self._lock:
            try:
                await asyncio.to_thread(_atomic_write, self.path, data)
            except Exception as exc:
                log.error("Could not save state %s: %s", self.path, exc)


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
