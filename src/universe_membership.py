"""The buy-eligible universe: one leaf module with no imports, so the decision
stage and the admission symbol guard read the same set without an import cycle."""


def _norm(symbol) -> str:
    return str(symbol or "").strip().upper()


def buy_eligible_universe(configured, admitted=None, positions=None) -> set[str]:
    """The one buy-eligible universe for every run: configured names, today's
    admissions, and every name currently held.

    A held name is in the universe by definition — it was admitted when it was
    bought. Only the morning run fills today's admissions, so without the held
    names an insider-admitted holding fell out of every later run (intraday,
    midday, close, next morning) and was told it would not be bought today.
    Holdings still face the same entry rules; this only says they are in the
    universe those rules are applied to.
    """
    held = {_norm(p.get("symbol") if isinstance(p, dict) else getattr(p, "symbol", None)) for p in positions or []}
    names = {_norm(s) for s in configured or []} | {_norm(s) for s in admitted or []} | held
    names.discard("")
    return names
