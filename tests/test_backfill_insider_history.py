import json
from datetime import date
from pathlib import Path

import pandas as pd

from scripts import backfill_insider_history as bf
from src.data.smart_money import SECForm4Provider


def _tables():
    d = date.today().strftime("%d-%b-%Y").upper()
    return {
        "SUBMISSION": pd.DataFrame(
            {
                "ACCESSION_NUMBER": ["A1", "A2", "A3"],
                "DOCUMENT_TYPE": ["4", "4", "3"],
                "ISSUERTRADINGSYMBOL": ["brk.b", "XYZ", "XYZ"],
                "FILING_DATE": [d, d, d],
            }
        ),
        "REPORTINGOWNER": pd.DataFrame(
            {"ACCESSION_NUMBER": ["A1", "A1", "A2", "A3"], "RPTOWNERCIK": ["0000123", "0000999", "0000456", "0000789"]}
        ),
        "NONDERIV_TRANS": pd.DataFrame(
            {
                "ACCESSION_NUMBER": ["A1", "A1", "A2", "A2", "A3"],
                "TRANS_DATE": [d, d, d, d, d],
                "TRANS_CODE": ["P", "A", "S", "S", "P"],
                "TRANS_ACQUIRED_DISP_CD": ["A", "A", "D", "A", "A"],
            }
        ),
    }


def _expected(tmp_path: Path, rows):
    p = SECForm4Provider(data_dir=str(tmp_path / "ref"))
    p._record_history(rows)
    return json.loads(p.stores.history_path.read_text())


def test_mapped_records_equal_record_history(tmp_path):
    rows = bf.rows_from_form345(_tables())
    iso = date.today().isoformat()
    # first owner only, P/A and S/D only, form 4 only, dotted symbol normalised
    assert sorted((r["actor_cik"], r["symbol"], r["direction"]) for r in rows) == [
        ("123", "BRK-B", "buy"),
        ("456", "XYZ", "sell"),
    ]
    got = bf.merge_rows(rows, None, writable=False)
    live = [
        {"actor_cik": "123", "symbol": "BRK-B", "direction": "buy", "transaction_date": iso},
        {"actor_cik": "456", "symbol": "XYZ", "direction": "sell", "transaction_date": iso},
    ]
    assert got == _expected(tmp_path, live)


def test_merge_idempotent(tmp_path):
    rows = bf.rows_from_form345(_tables())
    path = tmp_path / "insider_history.json"
    bf.main(["--write", str(path)], rows=rows)
    first = path.read_text()
    bf.main(["--write", str(path)], rows=rows)
    assert path.read_text() == first


def test_dry_run_writes_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert bf.main([], rows=bf.rows_from_form345(_tables())) == 0
    assert bf.main(["--dry-run"], rows=bf.rows_from_form345(_tables())) == 0
    assert list(tmp_path.rglob("insider_history.json")) == []
    assert "nothing written" in capsys.readouterr().out


def test_discover_zip_urls_reads_both_paths():
    html = (
        '<a href="/files/structureddata/data/insider-transactions-data-sets/2022q4_form345.zip">x</a>'
        '<a href="/files/structureddata/data/insider-transactions-data-sets/2023q1_form345.zip">x</a>'
        '<a href="/files/datastandardsinnovation/data/insider-transactions-data-sets/2026q2_form345.zip">x</a>'
    )
    assert list(bf.discover_zip_urls(html)) == ["2023q1", "2026q2"]
    assert "datastandardsinnovation" in bf.discover_zip_urls(html)["2026q2"]


def test_trade_dated_after_its_filing_is_dropped():
    t = _tables()
    t["NONDERIV_TRANS"]["TRANS_DATE"] = ["01-JAN-2028"] + [t["NONDERIV_TRANS"]["TRANS_DATE"][1]] * 4
    rows = bf.rows_from_form345(t)
    assert [(r["actor_cik"], r["symbol"]) for r in rows] == [("456", "XYZ")]
