"""One-off backfill of the insider (owner, issuer) trade-date index.

Why: the live fetch reads only ``filings.recent`` so ``insider_history.json``
starts effectively in 2025, yet the routine classifier needs three preceding
calendar years (Cohen-Malloy-Pomorski). This reads the SEC "Insider
Transactions Data Sets" quarterly zips and merges them through the provider's
own ``_record_history`` (same key shape, merge and prune).

Default is a dry run that writes nothing. ``--write PATH`` merges into PATH.

Known gap vs the live path: the live parser also drops issuers not on the
exchange-listed ticker file; the data sets carry no exchange, so only the
symbol is kept here.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import tempfile
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import pandas as pd
import requests

from src.config.smart_money import SmartMoneyConfig
from src.data.smart_money import SECForm4Provider
from src.data.smart_money_stores import atomic_json, load_json

PAGE_URL = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
SITE = "https://www.sec.gov"
# Same User-Agent the desk's live SEC fetch uses (config.smart_money.user_agent).
USER_AGENT = SmartMoneyConfig().user_agent
MIN_INTERVAL_S = 0.2  # 5 req/s, half of SEC's 10/s limit
FIRST_YEAR = 2023
_ZIP_RE = re.compile(r'href="([^"]*?/((\d{4})q([1-4])_form345\.zip))"')


def discover_zip_urls(page_html: str, first_year: int = FIRST_YEAR) -> dict[str, str]:
    """Map ``YYYYqN`` to its absolute URL, read from the SEC page (never guessed)."""
    found: dict[str, str] = {}
    for href, _name, year, quarter in _ZIP_RE.findall(page_html):
        if int(year) >= first_year:
            found[f"{year}q{quarter}"] = href if href.startswith("http") else SITE + href
    return dict(sorted(found.items()))


def rows_from_form345(tables: dict[str, pd.DataFrame]) -> list[dict]:
    """Non-derivative open-market P/S trades as the rows ``_record_history`` takes."""
    sub = tables["SUBMISSION"]
    sub = sub[sub["DOCUMENT_TYPE"].astype(str).str.startswith("4")]
    trans = tables["NONDERIV_TRANS"]
    owners = tables["REPORTINGOWNER"].drop_duplicates("ACCESSION_NUMBER", keep="first")
    merged = trans.merge(sub[["ACCESSION_NUMBER", "ISSUERTRADINGSYMBOL"]], on="ACCESSION_NUMBER").merge(
        owners[["ACCESSION_NUMBER", "RPTOWNERCIK"]], on="ACCESSION_NUMBER"
    )
    code = merged["TRANS_CODE"].astype(str).str.upper()
    side = merged["TRANS_ACQUIRED_DISP_CD"].astype(str).str.upper()
    # Same pairing the live parser enforces: P must be Acquired, S Disposed.
    keep = ((code == "P") & (side == "A")) | ((code == "S") & (side == "D"))
    merged = merged[keep].copy()
    merged["code"] = code[keep]
    merged["day"] = pd.to_datetime(merged["TRANS_DATE"], format="%d-%b-%Y", errors="coerce")
    merged["cik"] = pd.to_numeric(merged["RPTOWNERCIK"], errors="coerce")
    merged = merged.dropna(subset=["day", "cik", "ISSUERTRADINGSYMBOL"])
    out = pd.DataFrame(
        {
            "actor_cik": merged["cik"].astype("int64").astype(str),
            "symbol": merged["ISSUERTRADINGSYMBOL"].astype(str).str.strip().str.upper().str.replace(".", "-"),
            "direction": merged["code"].map({"P": "buy", "S": "sell"}),
            "transaction_date": merged["day"].dt.strftime("%Y-%m-%d"),
        }
    )
    return out.drop_duplicates().to_dict("records")


def read_zip(blob: bytes) -> list[dict]:
    names = ("SUBMISSION", "NONDERIV_TRANS", "REPORTINGOWNER")
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        tables = {n: pd.read_csv(zf.open(f"{n}.tsv"), sep="\t", dtype=str, low_memory=False) for n in names}
    return rows_from_form345(tables)


class _FileStores:
    """Stand-in for the provider's stores: one history file, atomic save."""

    def __init__(self, path: Path | None, *, writable: bool):
        self.path, self.writable = path, writable
        self.payload = load_json(path, {}) if path is not None and path.exists() else {}

    def load_history(self):
        return self.payload

    def save_history(self, payload: object) -> None:
        self.payload = payload
        if self.writable and self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(self.path, payload)


def merge_rows(rows: list[dict], path: Path | None, *, writable: bool) -> dict:
    """Merge via the provider's own ``_record_history``; returns the stored index."""
    with tempfile.TemporaryDirectory() as tmp:
        provider = SECForm4Provider(
            data_dir=tmp, insider_history_retention_days=SmartMoneyConfig().insider_history_retention_days
        )
        provider.stores = _FileStores(path, writable=writable)
        provider._record_history(rows)
        return provider.stores.payload


def coverage(index: dict) -> dict:
    per_year: dict[int, int] = defaultdict(int)
    insiders: dict[int, set] = defaultdict(set)
    for key, entries in index.items():
        owner = key.partition("|")[0]
        for entry in entries:
            year = int(entry[:4])
            per_year[year] += 1
            insiders[year].add(owner)
    return {
        "entries_per_year": dict(sorted(per_year.items())),
        "insiders_per_year": {y: len(s) for y, s in sorted(insiders.items())},
    }


def download_all(session: requests.Session) -> list[dict]:
    headers = {"User-Agent": USER_AGENT}
    page = session.get(PAGE_URL, headers=headers, timeout=60)
    page.raise_for_status()
    rows: list[dict] = []
    for label, url in discover_zip_urls(page.text).items():
        time.sleep(MIN_INTERVAL_S)
        resp = session.get(url, headers=headers, timeout=300)
        resp.raise_for_status()
        got = read_zip(resp.content)
        print(f"{label}: {len(got)} trades", file=sys.stderr)
        rows.extend(got)
    return rows


def main(argv: list[str] | None = None, rows: list[dict] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", type=Path, default=None, help="merge into this insider_history.json (default: dry run)")
    ap.add_argument("--dry-run", action="store_true", help="print counts, write nothing (default)")
    args = ap.parse_args(argv)
    if args.write is not None and args.dry_run:
        ap.error("--write and --dry-run are mutually exclusive")
    if rows is None:
        rows = download_all(requests.Session())
    index = merge_rows(rows, args.write, writable=args.write is not None)
    cov = coverage(index)
    print(f"mapped trades: {len(rows)}")
    print(f"entries per year: {cov['entries_per_year']}")
    print(f"insiders with trades per year: {cov['insiders_per_year']}")
    print(f"written: {args.write}" if args.write is not None else "dry run: nothing written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
