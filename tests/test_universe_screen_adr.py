"""Owner ruling 2026-10-10: foreign ADRs are in the universe; preferreds stay out."""

from __future__ import annotations

import pytest

from src.data.nasdaq_listing import _is_preferred
from src.universe_screen import is_non_common_security

# Real Nasdaq / NYSE listing names.
REAL_ADRS = [
    ("ARM", "Arm Holdings plc - American Depositary Shares"),
    ("BABA", "Alibaba Group Holding Limited American Depositary Shares each representing eight Ordinary share"),
    ("SHEL", "Shell PLC American Depositary Shares (each representing two (2) Ordinary Shares) "),
    ("PDD", "PDD Holdings Inc. - American Depositary Shares"),
    ("SE", "Sea Limited American Depositary Shares, each representing one Class A Ordinary Share"),
    ("ARGX", "argenx SE - American Depositary Shares"),
    ("BTI", "British American Tobacco  Industries, p.l.c. Common Stock ADR"),
    ("BHP", "BHP Group Limited American Depositary Shares (Each representing two Ordinary Shares)"),
    (
        "SIMO",
        "Silicon Motion Technology Corporation - American Depositary Shares, each representing four ordinary shares",
    ),
    ("INFY", "Infosys Limited American Depositary Shares"),
    ("SKHY", "SK hynix Inc. - American Depositary Shares"),
    ("AAPG", "Ascentage Pharma Group International - American Depository Shares"),
    ("API", "Agora, Inc. - ADS"),
    (
        "OMAB",
        "Grupo Aeroportuario del Centro Norte S.A.B. de C.V. - American "
        "Depositary Shares each representing 8 Series B shares",
    ),
    (
        "RLX",
        "RLX Technology Inc. American Depositary Shares, each representing the "
        "right to receive one (1) Class A ordinary share",
    ),
]

REAL_NOT_COMMON = [
    (
        "ANG$D",
        "American National Group Inc. Depositary Shares, each representing a "
        "1/1000th interest in a share of 7.375% Fixed-Rate Non-Cumulative "
        "Preferred Stock, Series D",
    ),
    ("GS$D", "Goldman Sachs Group, Inc. (The) Dep Shs repstg 1/1000 Pfd Ser D Fltg"),
    ("CDZIP", "Cadiz, Inc. - Depositary Shares"),
    ("MET$E", "MetLife, Inc. Depositary Shares "),
    ("ITUB", "Itau Unibanco Banco Holding SA American Depositary Shares (Each repstg 500 Preferred shares)"),
    (
        "KOF",
        "Coca Cola Femsa S.A.B. de C.V.  American Depositary Shares, each "
        "representing 10 Units (each Unit consists of 3 Series B Shares and 5 "
        "Series L Shares)",
    ),
    ("IQMXW", "IQM Quantum Computers Oyj - Warrants to purchase American Depositary Shares"),
]


@pytest.mark.parametrize(("symbol", "name"), REAL_ADRS)
def test_real_adr_names_are_admitted(symbol, name):
    assert is_non_common_security(symbol, name) is False
    assert _is_preferred(symbol, name) is False


@pytest.mark.parametrize(("symbol", "name"), REAL_NOT_COMMON)
def test_real_preferred_depositary_and_other_names_still_refused(symbol, name):
    assert is_non_common_security(symbol, name) is True


@pytest.mark.parametrize(("symbol", "name"), REAL_NOT_COMMON[:5])
def test_preferred_depositary_names_still_flagged_preferred(symbol, name):
    assert _is_preferred(symbol, name) is True
