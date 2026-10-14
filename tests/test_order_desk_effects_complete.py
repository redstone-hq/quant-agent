"""Every guarded-failure site the order desk names must say what its failure means.

`mark(desk, site, exc)` looks the site up in `EFFECT`; a site without an entry
makes the exception handler itself raise KeyError, so the caller never gets the
failure result it was promised. A missing entry must fail here, in tests, never
be papered over at run time.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import MagicMock

from src.execution.broker_parts.order_desk import OrderDesk
from src.execution.broker_parts.order_desk_effects import EFFECT

_EXECUTION = Path(__file__).resolve().parent.parent / "src" / "execution"
_SITE_CALLS = {"mark", "ok"}


def _desk(client) -> OrderDesk:
    params = inspect.signature(OrderDesk).parameters
    kwargs = {name: MagicMock(name=name) for name in params if params[name].default is inspect.Parameter.empty}
    kwargs["client"] = client
    return OrderDesk(**kwargs)


def test_refused_quantity_shrink_returns_false_and_detail_not_keyerror():
    client = MagicMock(name="client")
    client.replace_order_by_id.side_effect = RuntimeError("422 order is not replaceable")
    done, detail = _desk(client).replace_order_qty("ord-1", 5)
    assert done is False
    assert "RuntimeError" in detail
    assert "422 order is not replaceable" in detail
    client.replace_order_by_id.assert_called_once()


def test_failed_open_order_listing_returns_false_and_empty_not_keyerror():
    client = MagicMock(name="client")
    client.get_orders.side_effect = RuntimeError("503 gateway")
    assert _desk(client).list_open_orders_checked() == (False, [])


def test_accepted_quantity_shrink_returns_true():
    client = MagicMock(name="client")
    assert _desk(client).replace_order_qty("ord-1", 5) == (True, "")


def _named_sites() -> dict[str, list[str]]:
    """Every literal site name passed as 2nd argument to mark(...) / ok(...) under src/execution."""
    sites: dict[str, list[str]] = {}
    for path in sorted(_EXECUTION.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            func = node.func
            called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if called not in _SITE_CALLS:
                continue
            site = node.args[1]
            assert isinstance(site, ast.Constant) and isinstance(site.value, str), (
                f"{path.name}:{node.lineno} passes a non-literal site name to {called}(); "
                "use a string literal so the test can check it has an EFFECT entry"
            )
            sites.setdefault(site.value, []).append(f"{path.name}:{node.lineno}")
    return sites


def test_the_scan_finds_the_known_sites():
    sites = _named_sites()
    assert "replace_order_qty" in sites
    assert "cancel_entry_order" in sites
    assert len(sites) >= 10


def test_every_named_site_has_an_effect_entry():
    missing = {site: where for site, where in _named_sites().items() if site not in EFFECT}
    assert not missing, f"sites passed to mark()/ok() with no EFFECT entry: {missing}"


def test_no_effect_entry_is_blank():
    assert all(text.strip() for text in EFFECT.values())
