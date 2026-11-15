"""Plugin packages (sources, generators, publishers) plus tiny helpers they share.

Nothing in ``core/`` imports a specific plugin: modules placed in the
sub-packages are discovered at startup by ``core.registry.PluginRegistry``.
"""

from __future__ import annotations

_CURRENCY_SYMBOLS = {"ILS": "₪", "USD": "$", "EUR": "€", "GBP": "£"}


def format_price(amount: float, currency: str) -> str:
    """Human-friendly price: ``₪189.90`` for known currencies, ``189.90 CHF`` otherwise."""
    symbol = _CURRENCY_SYMBOLS.get(currency.upper())
    return f"{symbol}{amount:,.2f}" if symbol else f"{amount:,.2f} {currency.upper()}"
