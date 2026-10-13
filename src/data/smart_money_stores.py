"""The Form 4 provider's on-disk files, owned in one place.

Every read and write of the manifest, the observation cache, the insider
history index, the SEC ticker/exchange cache and the raw filing cache goes
through this object. It takes its paths by value and knows nothing of the
provider, the network or the rate limiter.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from src.trading_calendar import utc_now

logger = logging.getLogger(__name__)


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(tmp, path)


def load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text()) if path.exists() else fallback
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("Smart-money cache unreadable at %s: %s", path, exc)
        return fallback


class SmartMoneyStores:
    def __init__(
        self,
        *,
        manifest_path: Path,
        observations_path: Path,
        history_path: Path,
        tickers_path: Path,
        raw_dir: Path,
    ):
        self.manifest_path = manifest_path
        self.observations_path = observations_path
        self.history_path = history_path
        self.tickers_path = tickers_path
        self.raw_dir = raw_dir

    def load_manifest(self):
        return load_json(self.manifest_path, {})

    def save_manifest(self, payload: object) -> None:
        atomic_json(self.manifest_path, payload)

    def load_observations(self):
        return load_json(self.observations_path, [])

    def save_observations(self, rows: object) -> None:
        atomic_json(self.observations_path, rows)

    def load_history(self):
        return load_json(self.history_path, {})

    def save_history(self, payload: object) -> None:
        atomic_json(self.history_path, payload)

    def tickers_cached(self) -> bool:
        return self.tickers_path.exists()

    def tickers_stale(self) -> bool:
        try:
            return (
                not self.tickers_path.exists() or utc_now().timestamp() - self.tickers_path.stat().st_mtime > 24 * 3600
            )
        except OSError:
            return True

    def load_tickers(self):
        return load_json(self.tickers_path, {})

    def save_tickers(self, payload: object) -> None:
        atomic_json(self.tickers_path, payload)

    def cached_filing(self, accession: str) -> str | None:
        path = self.raw_dir / f"{accession}.txt"
        if path.exists():
            return path.read_text(errors="replace")
        return None

    def save_filing(self, accession: str, content: bytes) -> None:
        path = self.raw_dir / f"{accession}.txt"
        tmp = path.with_suffix(".txt.tmp")
        tmp.write_bytes(content)
        os.replace(tmp, path)


def build_smart_money_stores(data_dir: Path) -> SmartMoneyStores:
    data_dir = Path(data_dir)
    raw_dir = data_dir / "filings"
    data_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    return SmartMoneyStores(
        manifest_path=data_dir / "manifest.json",
        observations_path=data_dir / "observations.json",
        history_path=data_dir / "insider_history.json",
        tickers_path=data_dir / "company_tickers_exchange.json",
        raw_dir=raw_dir,
    )
