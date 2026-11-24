"""Exact decimal storage that behaves the same on SQLite and Postgres.

Django's DecimalField is stored with numeric affinity on SQLite, which can silently round to a
15-digit float. Money and coin amounts here are stored as text and read back as `Decimal`, so a
value written is always the value read. Sums are done in Python, never in SQL.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import models


def to_dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        value = repr(value)  # shortest string that round-trips, e.g. 0.1 -> "0.1"
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValidationError(f"Not a number: {value!r}") from exc


def dec_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    s = format(value.normalize(), "f")
    return "0" if s in ("-0", "") else s


class ExactDecimalField(models.CharField):
    description = "Decimal stored as exact text"

    def __init__(self, *args, **kwargs):
        from django.core.validators import MaxLengthValidator

        kwargs.setdefault("max_length", 64)
        super().__init__(*args, **kwargs)
        # values are Decimals in Python; the text length is checked by the database, not len(Decimal)
        self.validators[:] = [v for v in self.validators if not isinstance(v, MaxLengthValidator)]

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        if kwargs.get("max_length") == 64:
            kwargs.pop("max_length")
        return name, path, args, kwargs

    def from_db_value(self, value, expression, connection):
        return to_dec(value)

    def to_python(self, value):
        return to_dec(value)

    def get_prep_value(self, value):
        return dec_str(to_dec(value))

    def formfield(self, **kwargs):
        from django import forms

        defaults = {"form_class": forms.DecimalField, "max_digits": None, "decimal_places": None}
        defaults.update(kwargs)
        defaults.pop("max_length", None)
        return models.Field.formfield(self, **defaults)
