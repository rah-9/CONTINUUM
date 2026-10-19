# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Trial 6 fixture: the `invoicer` project, with one failing test (the bug to debug)."""

import sys
from pathlib import Path

FILES = {
    "invoicer/__init__.py": '"""Invoice totals for our supplier feeds."""\n',
    "invoicer/parse.py": '''"""Amounts as the supplier feeds send them."""

from decimal import Decimal


def parse_amount(text: str) -> Decimal:
    """'1,234.50' (US feed) or '1234,50' (EU feed) -> Decimal('1234.50')."""
    s = text.strip()
    if "," in s and "." not in s:
        s = s.replace(",", ".")          # EU decimal comma
    else:
        s = s.replace(",", "")           # US thousands separator
    return Decimal(s)
''',
    "invoicer/totals.py": '''"""Invoice totals."""

from decimal import ROUND_HALF_EVEN, Decimal

from invoicer.parse import parse_amount

CENT = Decimal("0.01")


def line_net(quantity: int, unit_price: str) -> Decimal:
    return quantity * parse_amount(unit_price)


def invoice_total(lines: list[tuple[int, str]], vat_rate: str) -> Decimal:
    """Gross total of (quantity, unit price) lines with VAT at ``vat_rate`` (e.g. '0.19')."""
    net = sum((line_net(q, p) for q, p in lines), Decimal(0))
    gross = net * (1 + Decimal(vat_rate))
    return gross.quantize(CENT, rounding=ROUND_HALF_EVEN)
''',
    "tests/test_totals.py": '''from decimal import Decimal

from invoicer.parse import parse_amount
from invoicer.totals import invoice_total


def test_us_amounts():
    assert parse_amount("1,234.50") == Decimal("1234.50")
    assert parse_amount("19.99") == Decimal("19.99")


def test_eu_simple():
    assert parse_amount("19,99") == Decimal("19.99")


def test_invoice_us():
    assert invoice_total([(2, "10.00"), (1, "5.00")], "0.19") == Decimal("29.75")


def test_invoice_eu():
    # the EU feed's larger amounts carry a dot as thousands separator
    assert invoice_total([(1, "1.234,50"), (2, "10,00")], "0.19") == Decimal("1492.86")
''',
    "README.md": "# invoicer\n\nInvoice totals for our US and EU supplier feeds. "
                 "Run the tests with `python -m pytest -q`.\n",
}


def write(root: Path) -> None:
    for name, text in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


if __name__ == "__main__":
    write(Path(sys.argv[1]))
