"""Per-provider semaphores and token-rate governors.

Moved out of src/agents/base.py; base.py re-exports every name. The governor's
input estimate and the "never sent" test live here beside the governors.
"""

import logging
import os
import threading
from src.token_budget import size_model_for_agent
from src.token_rate import TokenRateGovernor

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


# Per-provider in-flight caps around the LLM HTTP call itself (NOT around
# run() — building the user message / parsing must never hold a slot).
#
# Why: the relay enforces a per-user concurrency cap, and morning fans out
# macro + news + tech (multi-chunk) + earnings through a
# ThreadPoolExecutor(max_workers=4) on one relay account — the fan-out
# self-inflicted "Concurrency limit exceeded" 429 storms (175 occurrences in
# the 06-16..06-29 logs), and each kill-looped morning re-spawned the full
# team into the already-limited relay. A module-level semaphore serializes
# the excess instead of bouncing it off the server. Anthropic is direct
# (no relay) so it gets a looser, independent cap — failover calls must not
# queue behind a wedged relay slot.
_OPENAI_MAX_CONCURRENT = _int_env("QUANT_AGENT_MAX_CONCURRENT_LLM", 3)
_OPENAI_LLM_SEMAPHORE = threading.Semaphore(_OPENAI_MAX_CONCURRENT)
_ANTHROPIC_MAX_CONCURRENT = 4
_ANTHROPIC_LLM_SEMAPHORE = threading.Semaphore(_ANTHROPIC_MAX_CONCURRENT)
# OpenRouter is a distinct account/rate-limit domain from the OpenAI relay —
# it must not share (and be starved by, or starve) the relay's cap.
_OPENROUTER_MAX_CONCURRENT = _int_env("QUANT_AGENT_MAX_CONCURRENT_OPENROUTER", 3)
_OPENROUTER_LLM_SEMAPHORE = threading.Semaphore(_OPENROUTER_MAX_CONCURRENT)
# Google AI Studio direct is a third distinct account/rate-limit domain (its
# own free-tier RPM/TPM/RPD ceiling — see _GOOGLE_TOKENS_PER_MIN below) and
# must not share the OpenAI relay's or OpenRouter's cap. Default of 3 mirrors
# OpenRouter's; the free tier's 15 RPM ceiling leaves ample headroom for 3
# concurrent in-flight requests at the call latencies this desk sees.
_GOOGLE_MAX_CONCURRENT = _int_env("QUANT_AGENT_MAX_CONCURRENT_GOOGLE", 3)
_GOOGLE_LLM_SEMAPHORE = threading.Semaphore(_GOOGLE_MAX_CONCURRENT)

# --- tokens per minute, the limit the semaphores above never bounded -------
#
# A concurrency cap counts REQUESTS. A provider rate limit counts TOKENS per
# minute. Three concurrent 80,000-token requests satisfy a cap of three and
# are exactly what gets declined, which is how the Technical Analyst could
# send ~314,000 tokens in 80 seconds (~252k/min) while every cap in the
# system read as green. It was the only agent ever rate-limited: eleven times
# in three weeks, with no other agent declined once.
#
# This is a BACKSTOP, not the mechanism. The actual fix is that requests are
# now built to a size budget rather than cut to a fixed item count (see
# src/token_budget.py): the largest request a morning pass can produce fell
# from ~136,000 tokens to ~44,700, so even all three concurrent slots full
# is ~134k/min — under this ceiling by construction, not by luck.
#
# The ceiling exists for what the budget cannot see: a new agent, a prompt
# that grows, a retry storm. 150k/min sits below the ~252k/min burst that
# was actually being refused and above anything the packer can now emit, so
# in normal operation it must never fire. If it does, that is a bug report
# about something having grown — and it says exactly that, at CRITICAL.
#
# Deliberately NOT the primary control: pacing against a guessed ceiling
# means discovering the real limit by being refused, and every refusal is
# paid for. Sizing the request before it leaves costs nothing.
_OPENROUTER_TOKENS_PER_MIN = _int_env("QUANT_AGENT_OPENROUTER_TPM", 150_000)
_OPENAI_TOKENS_PER_MIN = _int_env("QUANT_AGENT_OPENAI_TPM", 150_000)
_ANTHROPIC_TOKENS_PER_MIN = _int_env("QUANT_AGENT_ANTHROPIC_TPM", 150_000)

# Google AI Studio direct (project qamc-gemini) free tier, READ OFF THE
# OWNER'S OWN AI STUDIO DASHBOARD 2026-08-31: 15 RPM / 250,000 TPM / 500 RPD.
# Published blog figures (1,000-1,500 RPD) are WRONG for this project; the
# dashboard is authoritative. TPM is the BINDING constraint, not RPD: a
# morning pass is ~215,000 tokens — 86% of the per-minute ceiling in a single
# burst — while RPD at ~76 requests/day against 500 is a non-issue.
#
# 200,000 is 80% of that measured 250,000 TPM ceiling, not a guessed number —
# the owner's explicit objection to an earlier ceiling was that it was chosen
# by taste rather than derived from a published/measured limit. A 6-request
# test at ~253k tokens BREACHED the real ceiling (recorded on the dashboard,
# though the calls happened to still succeed) — "no error" is not "no
# limit", so this stays a genuine safety margin, not a number tuned to the
# last incident.
_GOOGLE_TOKENS_PER_MIN = _int_env("QUANT_AGENT_GOOGLE_TPM", 200_000)
_GOVERNOR_MAX_WAIT_S = float(_int_env("QUANT_AGENT_TPM_MAX_WAIT_S", 120))

# FALLBACK-ONLY pre-request size estimate, used for a seat/model pair whose
# own history is too thin for `src.token_budget` to fit (fewer than its
# MIN_SAMPLES calls) — see `_governed_input_estimate`. The dense numeric payload that actually trips
# rate limits tokenizes at roughly ONE token per character (measured: 314,366
# tokens for ~355,000 characters of OHLCV rows), nothing like the ~4 that
# prose gives. Estimating at 1.5 stays conservative for that case rather than
# under-charging the very requests the governor exists to bound; prose is
# over-charged, which only costs a little headroom on agents that send a
# twentieth as much. Every charge is reconciled to the provider's real usage
# the moment the response lands, so this constant only ever affects one
# request's wait decision, never the window's accuracy.
_GOVERNOR_CHARS_PER_TOKEN = 1.5

# Connect-phase failures: raised before a byte of the request is written.
# Matched by class name, as `llm_retry._RETRYABLE_EXC_NAMES` does, so the
# transport library is never imported here.
_NEVER_SENT_EXC_NAMES = frozenset({"ConnectError", "ConnectTimeout", "ConnectionRefusedError"})


def _governed_input_estimate(agent, model: str, user_message: str) -> int:
    """Input tokens to reserve for one request, before it is sent.

    Uses the seat's own fitted size model (`src.token_budget`: fixed tokens
    + tokens/byte, least squares over its paid calls in `agent_logs`) — the
    same model that already sizes the chart seat's chunks. The fit's x is
    the user-message bytes and its intercept absorbs the system prompt, so
    it is applied to the message alone. A pair with no fit falls back to
    `_GOVERNOR_CHARS_PER_TOKEN` over prompt + message (logged at debug).
    """
    fitted = size_model_for_agent(agent, default_model=model)
    if fitted.measured:
        return fitted.predict(len(user_message.encode("utf-8")))
    logger.debug(
        "token governor: no fitted size model for %s/%s; estimating input at %.1f chars/token",
        getattr(agent, "name", "?"),
        model,
        _GOVERNOR_CHARS_PER_TOKEN,
    )
    chars = len(agent.system_prompt) + len(user_message)
    return int(chars / _GOVERNOR_CHARS_PER_TOKEN)


def _never_left_process(exc: BaseException) -> bool:
    """True when the request provably never reached the provider.

    Only a failure to open the connection qualifies (refused, DNS, connect
    timeout): the transport raises `ConnectError`/`ConnectTimeout` before a
    byte of the request is written, and the SDKs wrap it as the cause. Anything
    later — a read timeout, a stream cut, a 5xx — may have reached the
    provider and keeps its input charge.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if type(cur).__name__ in _NEVER_SENT_EXC_NAMES:
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False


_TOKEN_GOVERNORS = {
    "openrouter": TokenRateGovernor(
        "OpenRouter",
        _OPENROUTER_TOKENS_PER_MIN,
        max_wait_s=_GOVERNOR_MAX_WAIT_S,
    ),
    "openai": TokenRateGovernor(
        "OpenAI",
        _OPENAI_TOKENS_PER_MIN,
        max_wait_s=_GOVERNOR_MAX_WAIT_S,
    ),
    "anthropic": TokenRateGovernor(
        "Anthropic",
        _ANTHROPIC_TOKENS_PER_MIN,
        max_wait_s=_GOVERNOR_MAX_WAIT_S,
    ),
    "deepseek": TokenRateGovernor(
        "DeepSeek",
        _int_env("QUANT_AGENT_DEEPSEEK_TPM", 150_000),
        max_wait_s=_GOVERNOR_MAX_WAIT_S,
    ),
    "google": TokenRateGovernor(
        "Google",
        _GOOGLE_TOKENS_PER_MIN,
        max_wait_s=_GOVERNOR_MAX_WAIT_S,
    ),
}


# finish/stop reasons that mean "output hit a ceiling mid-generation".
# Shared by the truncation flag in _execute() and the empty-content guards:
# an empty body WITH one of these reasons is a legitimate truncation (e.g. a
# reasoner burning the whole budget on CoT) that must surface as
# truncated=True, NOT trigger retry/failover (truncation never fails over —
# see CLAUDE.md). insufficient_system_resource is DeepSeek-specific: the
# inference system ran out of resources and returned a cut-off body on a 200.
