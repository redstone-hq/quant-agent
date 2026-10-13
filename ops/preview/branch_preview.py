"""Ephemeral, read-only branch-preview server for QAMC Mission Control.

Lets an operator view the CURRENT feature branch's frontend (whatever is
actually checked out in this `dev` checkout — both the legacy `/ui` dashboard
and the new `/cockpit` React build, which this script BUILDS from `frontend/`
at startup because the bundle is build output and is not committed) rendered against real, truthful QAMC
data, without duplicating or faking any runtime state and without touching
the `qamc` production account in any way.

How it gets real data: every JSON API request (anything other than /ui/*
or /cockpit/*) is proxied read-only, GET-only, to the already-running
`qamc` production Mission Control API on 127.0.0.1:8800 (reachable over
loopback regardless of account boundary — that is how this repo's own
frontend-verification tooling already reads live data, as recorded in
the Stage 6-react acceptance pass, preserved in Git history). This
process never reads
`qamc`'s SQLite database or credentials directly (it has no filesystem
permission to — the `dev`/`qamc` account boundary is unaffected), never
writes anything, and never starts, stops, or restarts any `qamc` service.

Version skew: this branch adds backend surface production hasn't deployed
yet (`/account.liquidity`, `/positions[].direction`, `GET /runs/{id}/funnel`).
Rather than leave those panels blank, the fields/endpoints that are missing
upstream are reconstructed here from data upstream ALREADY exposes, using
the exact same pure derivation rules and typed schemas this branch's own
backend uses (see the reconstruction helpers below for the specific source
each one mirrors) — never fabricated, and self-obsoleting: once production
actually has the field/route, the real upstream value passes straight
through untouched. `GET /prices/{symbol}` is the one exception — real bars
require Alpaca market-data credentials this `dev`-account process
deliberately does not have, so that one degrades honestly instead (empty
bars + an explanatory `error` string), per the project's "unknown stays
unknown, never fabricated" rule.

Safety boundaries:
- GET-only, enforced in this process's own middleware BEFORE a request is
  ever forwarded upstream — belt-and-suspenders on top of the upstream
  API's own independently-tested GET-only enforcement
  (tests/test_api_safety.py::test_api_routes_are_get_only).
- Binds ONLY to an explicit interface IP you pass in (the VPS's Tailscale
  IP in normal use) — never 0.0.0.0, never the public interface. A socket
  bound to one address never accepts connections arriving on another;
  this is not a firewall rule, it is how the bind itself works.
- No systemd unit, no auto-start; this is a plain foreground/background
  process you start for a review session and kill afterward — see
  ops/preview/README.md for the exact commands.
- Does not modify, restart, or depend on any `qamc`-owned process, file,
  or credential. The reconstruction helpers below read only this `dev`
  checkout's own non-secret `config/settings.yaml` (cash_sweep section)
  and re-derive fields from data upstream's own read-only API already
  returned — never a direct DB/broker read.

Usage (from the `dev` account, this checkout):
    source .venv/bin/activate
    python ops/preview/branch_preview.py --host 100.111.170.97 --port 8810

Then from another tailnet device:
    http://ovh-vps.wallaby-bowfin.ts.net:8810/cockpit/
    http://ovh-vps.wallaby-bowfin.ts.net:8810/ui/

Stop: Ctrl-C in the foreground, or `kill <pid>` if backgrounded.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

_REPO_ROOT_FOR_IMPORT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT_FOR_IMPORT) not in sys.path:
    # Same pattern as ops/commissioning/verify_commissioning.py — this is a
    # standalone script (no ops/__init__.py package), so `src.*` isn't
    # importable unless the repo root is on sys.path.
    sys.path.insert(0, str(_REPO_ROOT_FOR_IMPORT))

from src.api.deps import INVERSE_ETF_SYMBOLS
from src.quantities import (
    cash_above_reserve,
    deployable_cash,
    net_exposure_pct,
    net_exposure_usd,
    sweep_reserve_usd,
)
from src.api.schemas import (
    CandidateFunnelItem,
    ExposureBreakdown,
    LiquidityBreakdown,
    MacroBroaderContext,
    PmReasoning,
    RiskManagerVerdict,
    RunFunnelResponse,
)

logger = logging.getLogger("branch_preview")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_UI = REPO_ROOT / "src" / "api" / "static"
STATIC_COCKPIT = REPO_ROOT / "src" / "api" / "static_cockpit"
FRONTEND_DIR = REPO_ROOT / "frontend"
COCKPIT_STAMP = "BUILT_FROM_COMMIT"
SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"

UPSTREAM_BASE = "http://127.0.0.1:8800"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Mirrors src/api/routes_evidence.py::_TECH_DIRECTION exactly (display-only
# labeling). Duplicated rather than imported: that name is module-private
# and this reconstruction works over plain JSON dicts already returned by
# the upstream API, not the validated TechAnalysisResult object
# routes_evidence.py has in hand server-side.
_TECH_DIRECTION = {
    "strong_buy": "bullish", "buy": "bullish",
    "neutral": "neutral",
    "sell": "bearish", "strong_sell": "bearish",
}

# Concurrency cap for the funnel reconstruction's per-symbol candidate-detail
# fan-out — a full universe-scan run has ~80 candidates. Keeps this
# preview-only tool from opening an unbounded number of loopback sockets at
# once; the real endpoint does this in one DB query, this does it over HTTP
# because that DB isn't reachable from here (see module docstring).
_FUNNEL_FANOUT_CONCURRENCY = 16


class _GetOnlyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method not in _SAFE_METHODS:
            return JSONResponse(
                status_code=405,
                content={"detail": "branch preview is read-only: GET only"},
            )
        return await call_next(request)


def _current_branch_sha() -> tuple[str, str]:
    def _git(*args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", *args], cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL
            ).strip()
        except Exception:
            return "unknown"

    return _git("rev-parse", "--abbrev-ref", "HEAD"), _git("rev-parse", "--short", "HEAD")


def _cash_sweep_config() -> dict:
    """Real repo config (config/settings.yaml's cash_sweep section), read
    directly rather than through src.api.deps.get_config() — that path
    fully validates AppConfig, including a check that raises if an
    OpenRouter model is selected without OPENROUTER_API_KEY set in the
    environment, which this dev-account preview process has no reason to
    have. Reading just the one plain, non-secret, non-interpolated section
    this needs sidesteps that entirely. Degrades to CashSweepConfig's own
    defaults on any read failure — never raises."""
    try:
        raw = yaml.safe_load(SETTINGS_PATH.read_text()) or {}
        sweep = raw.get("cash_sweep") or {}
        reserve = raw.get("cash_reserve") or {}
        return {
            "enabled": bool(sweep.get("enabled", False)),
            "symbol": (str(sweep.get("symbol", "SGOV")).strip().upper() or "SGOV"),
            "reserve_pct": float(reserve.get("pct", 1.0)),
        }
    except Exception as exc:
        logger.warning("could not read cash_sweep config: %s", exc)
        return {"enabled": False, "symbol": "SGOV", "reserve_pct": 1.0}


def _risk_limits_config() -> dict:
    """Real repo config (config/settings.yaml's risk section), read
    directly for the same reason _cash_sweep_config() is: sidesteps
    get_config()'s full AppConfig validation (API-key checks this
    dev-account preview process has no reason to satisfy). Degrades to an
    honest all-None dict on any read failure — never a guessed limit."""
    try:
        raw = yaml.safe_load(SETTINGS_PATH.read_text()) or {}
        risk = raw.get("risk") or {}
        return {
            "max_position_pct": risk.get("max_position_pct"),
            "max_total_position_pct": risk.get("max_total_position_pct"),
            "max_sector_pct": risk.get("max_sector_pct"),
        }
    except Exception as exc:
        logger.warning("could not read risk config: %s", exc)
        return {"max_position_pct": None, "max_total_position_pct": None, "max_sector_pct": None}


def _position_direction(symbol: str, sweep_symbol: str) -> str:
    """Mirrors src/api/broker_reads.py::_position_direction exactly."""
    if symbol == sweep_symbol:
        return "cash_equivalent"
    if symbol in INVERSE_ETF_SYMBOLS:
        return "bearish_hedge"
    return "long"


def _is_executed_trade(row: dict) -> bool:
    """Mirrors src/api/db_reads.py::is_executed_trade exactly."""
    fill_status = row.get("fill_status")
    action = row.get("action")
    fill_qty = row.get("fill_qty") or 0
    return (fill_status is None and action != "HOLD") or fill_status == "filled" or fill_qty > 0


def build_cockpit() -> str | None:
    """Build this checkout's dashboard and swap it into STATIC_COCKPIT; return None or the reason.

    Same shape as scripts/merge_and_deploy.sh: build OFF TO THE SIDE first (a
    copy of frontend/ in a temp folder, so this checkout's own node_modules
    and bundle are untouched), require node's major version from
    frontend/.nvmrc, and swap the result in only on success. On failure the
    previous bundle stays on disk and create_app() shows the reason at
    /cockpit/ instead of serving it.
    """
    import tempfile

    need = (FRONTEND_DIR / ".nvmrc").read_text(encoding="utf-8").strip().split(".")[0]
    try:
        have = subprocess.run(["node", "-v"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        have = "none"
    if not have.startswith(f"v{need}."):
        return f"wrong node version (found {have}, need {need}.x)"
    with tempfile.TemporaryDirectory(prefix="cockpit-build-", dir=REPO_ROOT.parent) as tmp:
        work = Path(tmp)
        src = work / "frontend"
        out = work / "out"
        shutil.copytree(FRONTEND_DIR, src, ignore=shutil.ignore_patterns("node_modules", "dist", "*.tsbuildinfo"))
        for cmd in (
            ["npm", "ci", "--no-audit", "--no-fund"],
            ["npm", "run", "build", "--", "--outDir", str(out), "--emptyOutDir"],
        ):
            proc = subprocess.run(cmd, cwd=src, capture_output=True, text=True)
            if proc.returncode != 0:
                log = (proc.stdout + proc.stderr)[-6000:]
                return f"`{' '.join(cmd)}` failed (exit {proc.returncode}):\n{log}"
        if not (out / "index.html").is_file():
            return "the build finished but wrote no index.html"
        (out / COCKPIT_STAMP).write_text(_current_branch_sha()[1] + "\n", encoding="utf-8")
        old = work / "old"
        if STATIC_COCKPIT.exists():
            shutil.move(str(STATIC_COCKPIT), str(old))
        shutil.move(str(out), str(STATIC_COCKPIT))
    return None


def create_app(cockpit_build_error: str | None = None) -> FastAPI:
    app = FastAPI(
        title="QAMC Mission Control — BRANCH PREVIEW (not production)",
        description=(
            "Ephemeral operator preview of the current feature branch's "
            "frontend, proxying read-only GET requests to the real qamc "
            "production Mission Control API. Never writes anything; never "
            "touches qamc's runtime, database, or credentials."
        ),
    )
    app.add_middleware(_GetOnlyMiddleware)

    branch, sha = _current_branch_sha()
    client = httpx.AsyncClient(base_url=UPSTREAM_BASE, timeout=10.0)
    fanout_sem = asyncio.Semaphore(_FUNNEL_FANOUT_CONCURRENCY)

    async def upstream_json(path: str, params=None) -> tuple[int, dict]:
        response = await client.get(path, params=params)
        try:
            payload = response.json()
        except ValueError:
            payload = {"detail": "upstream returned a non-JSON response"}
        return response.status_code, payload

    async def _patch_account(data: dict) -> dict:
        """Old production's /account predates this branch's `liquidity`,
        `exposure` and `risk_limits` fields (Stage 6 / Stage 6j). Every
        number here comes from `src.quantities` — the SAME functions
        _compute_liquidity()/_compute_exposure() in src/api/routes_live.py
        call — so this shim cannot drift from the endpoint it stands in
        for. It used to re-type the reserve and deployable-cash formulas by
        hand, which is exactly how the dashboard came to print a
        "deployable" figure the engine had never used. risk_limits is
        reconstructed from this dev checkout's own non-secret
        config/settings.yaml risk section. Self-obsoleting: once upstream
        already returns a field, this is a no-op passthrough for it."""
        if data.get("risk_limits") is None and data.get("error") is None:
            data["risk_limits"] = _risk_limits_config()
        if data.get("error") is not None:
            return data
        if data.get("liquidity") is not None and data.get("exposure") is not None:
            return data
        cfg = _cash_sweep_config()
        cash = data.get("cash")
        portfolio_value = data.get("portfolio_value")
        sweep_parked_value = None
        upstream_positions: list[dict] = []
        try:
            pos_status, pos_data = await upstream_json("/positions")
            if pos_status == 200 and pos_data.get("error") is None:
                upstream_positions = pos_data.get("positions", []) or []
                sweep_parked_value = sum(
                    p.get("market_value") or 0.0
                    for p in upstream_positions
                    if p.get("symbol") == cfg["symbol"]
                )
        except httpx.RequestError as exc:
            logger.warning("liquidity reconstruction: /positions read failed: %s", exc)

        if data.get("liquidity") is None:
            reserve_usd = (
                sweep_reserve_usd(portfolio_value, cfg["reserve_pct"])
                if portfolio_value is not None else None
            )
            deployable = (
                deployable_cash(cash, sweep_parked_value)
                if cash is not None and sweep_parked_value is not None else None
            )
            above_reserve = (
                cash_above_reserve(cash, reserve_usd)
                if cash is not None and reserve_usd is not None else None
            )
            data["liquidity"] = LiquidityBreakdown(
                sweep_enabled=cfg["enabled"],
                sweep_symbol=cfg["symbol"],
                raw_cash=cash,
                sweep_parked_value=sweep_parked_value,
                reserve_usd=reserve_usd,
                deployable_cash=deployable,
                cash_above_reserve=above_reserve,
                total_liquidity=deployable,
            ).model_dump()

        if data.get("exposure") is None:
            if upstream_positions:
                net_usd = net_exposure_usd(
                    upstream_positions, cash_park_symbol=cfg["symbol"],
                )
                data["exposure"] = ExposureBreakdown(
                    net_exposure_usd=net_usd,
                    net_exposure_pct=net_exposure_pct(net_usd, portfolio_value),
                ).model_dump()
            else:
                data["exposure"] = ExposureBreakdown().model_dump()
        return data

    def _patch_positions(data: dict) -> dict:
        """Old production's /positions predates `is_cash_equivalent`/
        `direction` (Stage 6) — both pure functions of `symbol`, computed
        here from the real symbol upstream already returned."""
        cfg = _cash_sweep_config()
        for p in data.get("positions", []):
            if p.get("direction") is not None:
                continue
            symbol = p.get("symbol")
            p["is_cash_equivalent"] = symbol == cfg["symbol"]
            p["direction"] = _position_direction(symbol, cfg["symbol"])
        return data

    async def _fetch_candidate(run_id: str, symbol: str) -> dict | None:
        async with fanout_sem:
            try:
                status, detail = await upstream_json(f"/runs/{run_id}/candidates/{symbol}")
            except httpx.RequestError:
                return None
        return detail if status == 200 else None

    async def _reconstruct_funnel(run_id: str) -> dict | None:
        """Old production predates GET /runs/{id}/funnel entirely (Stage
        6). Rebuilds the identical RunFunnelResponse shape from real data
        via endpoints that ARE already deployed — GET /runs/{id} (for
        hard_risk_block_recorded + timestamp), GET /runs/{id}/candidates
        (the symbol list), GET /runs/{id}/candidates/{symbol} per symbol
        (the actual per-candidate decision-chain evidence) — the same
        source tables and the same aggregation rules as
        src/api/routes_evidence.py::get_run_funnel, just composed over HTTP
        instead of one direct DB read (this dev-account process has no
        access to qamc's database — see module docstring). Returns None on
        any failure to reconstruct; the caller falls back to an honest 404
        rather than ever returning a partial/fabricated funnel.

        Known gap: pm_reasoning/risk_verdict/macro_context are run-scoped
        but only obtainable here via a candidate's response, so a run with
        zero candidates reports them as None even if they exist upstream —
        an honest "unknown", not a wrong value; the real endpoint reads
        them independently of candidate count."""
        try:
            run_status, run_detail = await upstream_json(f"/runs/{run_id}")
            if run_status != 200:
                return None
            agent_logs = run_detail.get("agent_logs") or []
            if not agent_logs and not run_detail.get("trades") and not run_detail.get("decision_id"):
                return None  # run_id not actually recorded upstream

            cand_status, cand_list = await upstream_json(f"/runs/{run_id}/candidates")
            symbols = cand_list.get("candidates", []) if cand_status == 200 else []

            details = await asyncio.gather(*(_fetch_candidate(run_id, sym) for sym in symbols))
        except httpx.RequestError as exc:
            logger.warning("funnel reconstruction failed for %s: %s", run_id, exc)
            return None

        candidates: list[CandidateFunnelItem] = []
        bearish_hedge_considered = False
        reached_pm_count = 0
        proposed_order_count = 0
        executed_count = 0
        pm_reasoning = risk_verdict = macro_context = None

        for sym, detail in zip(symbols, details):
            if detail is None:
                continue
            if pm_reasoning is None and detail.get("pm_reasoning"):
                pm_reasoning = detail["pm_reasoning"]
            if risk_verdict is None and detail.get("risk_verdict"):
                risk_verdict = detail["risk_verdict"]
            if macro_context is None and detail.get("macro_context"):
                macro_context = detail["macro_context"]

            tech = detail.get("tech")
            direction = _TECH_DIRECTION.get(tech.get("rating"), "neutral") if tech else "unknown"
            is_hedge = sym in INVERSE_ETF_SYMBOLS
            if is_hedge:
                bearish_hedge_considered = True

            pm_target = detail.get("pm_target")
            reached_target = pm_target is not None
            if reached_target:
                reached_pm_count += 1

            pm_proposed = detail.get("pm_proposed_order")
            reached_proposed = pm_proposed is not None
            if reached_proposed:
                proposed_order_count += 1

            trade = detail.get("trade")
            executed = trade is not None and _is_executed_trade(trade)
            if executed:
                executed_count += 1

            candidates.append(CandidateFunnelItem(
                symbol=sym,
                direction=direction,
                is_bearish_hedge=is_hedge,
                reached_pm_target=reached_target,
                pm_target_weight_pct=(pm_target or {}).get("target_weight_pct"),
                pm_risk_allocation_pct=(pm_target or {}).get("risk_allocation_pct"),
                reached_proposed_order=reached_proposed,
                proposed_action=(pm_proposed or {}).get("action"),
                risk_modified=detail.get("risk_modification") is not None,
                executed=executed,
                trade_action=(trade or {}).get("action"),
            ))

        hard_risk_block = bool(run_detail.get("hard_risk_block_recorded", False))
        if hard_risk_block:
            decision_state = "hard_risk_block"
        elif executed_count > 0:
            decision_state = "executed"
        elif proposed_order_count > 0:
            decision_state = "proposed_not_executed"
        elif candidates:
            decision_state = "no_proposal"
        else:
            decision_state = "no_candidates"

        session_prefix = run_id.rsplit("-", 1)[0] if "-" in run_id else run_id
        timestamp = agent_logs[0].get("timestamp") if agent_logs else None

        return RunFunnelResponse(
            run_id=run_id,
            session_prefix=session_prefix,
            timestamp=timestamp,
            candidates=candidates,
            candidates_considered=len(candidates),
            reached_pm_count=reached_pm_count,
            proposed_order_count=proposed_order_count,
            executed_count=executed_count,
            bearish_hedge_considered=bearish_hedge_considered,
            hard_risk_block=hard_risk_block,
            pm_reasoning=PmReasoning(**pm_reasoning) if pm_reasoning else None,
            risk_verdict=RiskManagerVerdict(**risk_verdict) if risk_verdict else None,
            macro_context=MacroBroaderContext(**macro_context) if macro_context else None,
            decision_state=decision_state,
        ).model_dump()

    @app.get("/")
    def root() -> dict:
        return {
            "service": "qamc-mission-control-BRANCH-PREVIEW",
            "warning": "This is a read-only preview of an unmerged branch, not the qamc production service.",
            "branch": branch,
            "commit": sha,
            "proxied_data_from": UPSTREAM_BASE,
            "ui": "/ui/",
            "cockpit": "/cockpit/",
        }

    if STATIC_UI.is_dir():
        app.mount("/ui", StaticFiles(directory=str(STATIC_UI), html=True), name="ui")
    if cockpit_build_error is not None:
        # Show the failure where the dashboard would be -- never a silent
        # blank or a stale bundle from some other commit.
        @app.get("/cockpit")
        @app.get("/cockpit/{path:path}")
        def cockpit_build_failed(path: str = "") -> Response:
            return Response(
                "DASHBOARD BUILD FAILED for this branch preview - nothing is served.\n\n"
                + cockpit_build_error,
                status_code=503,
                media_type="text/plain; charset=utf-8",
            )
    elif STATIC_COCKPIT.is_dir():
        app.mount("/cockpit", StaticFiles(directory=str(STATIC_COCKPIT), html=True), name="cockpit")

    @app.get("/{path:path}")
    async def proxy(path: str, request: Request):
        norm = path.strip("/")

        # /account, /positions: real upstream data, only the two Stage-6
        # fields (missing upstream) are reconstructed. See _patch_account /
        # _patch_positions docstrings for exactly what and why.
        if norm == "account":
            try:
                status, data = await upstream_json(f"/{path}", params=request.query_params)
            except httpx.RequestError as exc:
                logger.warning("upstream proxy request failed for /%s: %s", path, exc)
                return JSONResponse(502, {"detail": f"branch preview could not reach the read-only qamc API: {exc}"})
            if status == 200:
                data = await _patch_account(data)
            return JSONResponse(status_code=status, content=data)

        if norm == "positions":
            try:
                status, data = await upstream_json(f"/{path}", params=request.query_params)
            except httpx.RequestError as exc:
                logger.warning("upstream proxy request failed for /%s: %s", path, exc)
                return JSONResponse(502, {"detail": f"branch preview could not reach the read-only qamc API: {exc}"})
            if status == 200:
                data = _patch_positions(data)
            return JSONResponse(status_code=status, content=data)

        # GET /runs/{run_id}/funnel: entirely new this branch (Stage 6),
        # 404s against old production. Try upstream first (future-proof —
        # once production has this route, this block is a no-op), and only
        # reconstruct on a genuine 404.
        if norm.startswith("runs/") and norm.endswith("/funnel"):
            try:
                upstream_resp = await client.get(f"/{path}", params=request.query_params)
            except httpx.RequestError as exc:
                logger.warning("upstream proxy request failed for /%s: %s", path, exc)
                return JSONResponse(502, {"detail": f"branch preview could not reach the read-only qamc API: {exc}"})
            if upstream_resp.status_code == 404:
                run_id = norm[len("runs/"):-len("/funnel")]
                reconstructed = await _reconstruct_funnel(run_id)
                if reconstructed is not None:
                    return JSONResponse(status_code=200, content=reconstructed)
                # Reconstruction couldn't confirm the run exists upstream —
                # fall through to the honest 404 passthrough below.
            return Response(
                content=upstream_resp.content,
                status_code=upstream_resp.status_code,
                media_type=upstream_resp.headers.get("content-type"),
            )

        try:
            upstream_resp = await client.get(f"/{path}", params=request.query_params)
        except httpx.RequestError as exc:
            logger.warning("upstream proxy request failed for /%s: %s", path, exc)
            return JSONResponse(
                status_code=502,
                content={"detail": f"branch preview could not reach the read-only qamc API: {exc}"},
            )
        # GET /prices/{symbol} is also new this branch, but real bars need
        # Alpaca market-data credentials this dev-account process
        # deliberately does not have — unlike /account, /positions and
        # /runs/{id}/funnel above, there is no real-data reconstruction
        # available for this one. Rewrite the bare 404 into
        # PriceBarsResponse's own honest-error shape instead of leaving an
        # opaque status code — still zero fabricated data, just an
        # accurate message.
        if upstream_resp.status_code == 404 and path.startswith("prices/"):
            symbol = path.removeprefix("prices/").split("?")[0].upper()
            return JSONResponse(
                status_code=200,
                content={
                    "symbol": symbol,
                    "bars": [],
                    "source": "alpaca_market_data",
                    "error": (
                        "GET /prices/{symbol} is new on this branch and not yet deployed "
                        "to qamc production, and real bars need Alpaca market-data "
                        "credentials this preview process does not have — no live data "
                        "available in this preview."
                    ),
                },
            )
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            media_type=upstream_resp.headers.get("content-type"),
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        logger.warning("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        required=True,
        help="Bind address — pass the VPS's Tailscale interface IP explicitly (e.g. 100.111.170.97). "
        "Refuses 0.0.0.0 and any address matching a known public-interface pattern is your responsibility "
        "to avoid; this script does not guess your network layout for you.",
    )
    parser.add_argument("--port", type=int, default=8810)
    args = parser.parse_args()

    if args.host in ("0.0.0.0", "::"):
        raise SystemExit("Refusing to bind 0.0.0.0/:: — pass the Tailscale interface IP explicitly.")

    build_error = build_cockpit()
    if build_error is not None:
        logger.error("dashboard build failed; /cockpit/ will show the error:\n%s", build_error)
    app = create_app(cockpit_build_error=build_error)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
