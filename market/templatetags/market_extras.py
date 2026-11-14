from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def price(value) -> str:
    """Plain decimal without trailing zeros or exponent: 78000.000 -> 78000, 0.0000012300 -> 0.00000123."""
    if value is None or value == "":
        return "—"
    d = Decimal(value).normalize()
    return format(d, "f")


@register.filter
def fprice(value) -> str:
    """Float price with sensible decimals for its size: 65815.3 -> 65,815.30; 0.000012345 -> 0.000012345."""
    import math

    if value is None or value == "":
        return "—"
    x = float(value)
    if x == 0 or not math.isfinite(x):
        return f"{x:,.2f}"
    decimals = min(10, max(2, 4 - math.floor(math.log10(abs(x)))))
    return f"{x:,.{decimals}f}"


@register.filter
def get_item(mapping, key):
    """dict lookup with a variable key: {{ d|get_item:k }}."""
    return mapping.get(key) if mapping else None
