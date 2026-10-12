"""Wiring for `PortfolioConstructor`: builds its two held parts and their delegates.

Construction only -- no behaviour lives here. `hold_parts` gives one
constructor a `StopRules` and an `OrderBuilders`; `install_delegates` puts a
same-named thin shim for every part method on the owner class, so each
existing call site (`constructor._resolve_stop(...)`, `self._build_buy(...)`)
keeps resolving and a name patched on the owner INSTANCE is what the parts see.
Anything that can change after construction is handed in live: the config
through a getter, the entry-builder collaborators and the `EntryStopResolver`
through late-bound callables that read the owner at call time. A part is never
handed the owner's delegate for a body it owns (that would recurse): the
resolver gets `_widen_stop_past_noise` ONLY when the owner's attribute is not
the owner class's own shim for it.
"""

from __future__ import annotations

from src.portfolio_constructor.entry_stop.resolver import EntryStopResolver
from src.portfolio_constructor.order_build.exits import ExitOrderBuilders
from src.portfolio_constructor.orders import _ORDER_BUILDER_COLLABORATORS, OrderBuilders
from src.portfolio_constructor.shim_guard import _is_class_shim
from src.portfolio_constructor.stops import StopRules

#: Owner names delegated to the held `StopRules`.
STOP_DELEGATES = (
    "_resolve_entry_and_stop",
    "_stop_atr_multiple",
    "_level_backing_stop",
    "_derive_structural_stop_no_atr",
    "_reward_risk_at",
    "real_reward_risk_preview",
    "entry_stop_preview",
    "_widen_stop_past_noise",
    "shipped_stop_rule",
    "shipped_stop_level_basis",
    "_resolve_stop",
)
#: Owner names delegated to the held `OrderBuilders`.
ORDER_DELEGATES = ("_long_entry_builder", "_short_entry_builder", "_build_buy", "_build_short")
#: Collaborator-free exits: static shims straight to the lifted bodies.
STATIC_EXIT_DELEGATES = ("_hold_decision", "_build_sell", "_build_cover")


def order_builder_collaborators(owner) -> dict:
    """The entry builders' keyword arguments, read off the owner NOW."""
    return {param: getattr(owner, attr) for param, attr in _ORDER_BUILDER_COLLABORATORS}


def build_entry_stop_resolver(owner, delegate_owner: type) -> EntryStopResolver:
    """Build the standalone resolver from the owner's live collaborators; body
    moved verbatim from the former `_StopMixin._entry_stop_resolver`."""
    return EntryStopResolver(
        cfg=owner.cfg,
        derive_target=owner._derive_target,
        note_data_fault=owner._note_data_fault,
        note_refusal=owner._note_refusal,
        resolve_stop=owner._resolve_stop,
        unpriceable_symbols=owner._unpriceable_symbols,
        reward_risk_at=owner._reward_risk_at,
        derive_structural_stop_no_atr=owner._derive_structural_stop_no_atr,
        level_backing_stop=owner._level_backing_stop,
        stop_atr_multiple=owner._stop_atr_multiple,
        # A moved body passed back in would overwrite the resolver's own method
        # with a call back into it: pass one ONLY when it is not the owner's shim.
        **{
            kw: getattr(owner, attr)
            for kw, attr in (("widen_stop_past_noise", "_widen_stop_past_noise"),)
            if not _is_class_shim(getattr(owner, attr), attr, delegate_owner)
        },
    )


def hold_parts(owner, *, delegate_owner: type) -> None:
    """Build one `StopRules` and one `OrderBuilders` onto `owner`."""
    owner._stop_rules = StopRules(
        read_cfg=lambda: owner.cfg,
        entry_stop_resolver=lambda: build_entry_stop_resolver(owner, delegate_owner),
    )
    owner._order_builders = OrderBuilders(collaborators=lambda: order_builder_collaborators(owner))


def _delegate(part_attr: str, name: str, where: str):
    def shim(self, *args, **kwargs):
        return getattr(getattr(self, part_attr), name)(*args, **kwargs)

    shim.__name__, shim.__qualname__ = name, f"install_delegates.<locals>.{name}"
    shim.__doc__ = f"Thin shim: body lives in {where}."
    return shim


def _static_exit(name: str):
    def shim(*args, **kwargs):
        return getattr(ExitOrderBuilders, name)(*args, **kwargs)

    shim.__name__, shim.__qualname__ = name, f"install_delegates.<locals>.{name}"
    shim.__doc__ = "Thin shim: body moved to src/portfolio_constructor/order_build/exits.py."
    return staticmethod(shim)


def install_delegates(owner_cls: type) -> type:
    """Class decorator: give `owner_cls` a thin shim for every part method."""
    for name in STOP_DELEGATES:
        setattr(owner_cls, name, _delegate("_stop_rules", name, "src/portfolio_constructor/stops.py"))
    for name in ORDER_DELEGATES:
        setattr(owner_cls, name, _delegate("_order_builders", name, "src/portfolio_constructor/orders.py"))
    for name in STATIC_EXIT_DELEGATES:
        setattr(owner_cls, name, _static_exit(name))

    def _entry_stop_resolver(self):
        """Thin shim: builds the standalone resolver per call (src/portfolio_constructor/assembly.py)."""
        return build_entry_stop_resolver(self, owner_cls)

    owner_cls._entry_stop_resolver = _entry_stop_resolver
    return owner_cls
