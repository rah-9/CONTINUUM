# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Trial 6 judge: hidden tests over model B's repository. Only stated requirements count.

  python judge.py PROJECT_DIR   -> JSON {criterion: passed}, score
"""

import inspect
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(root))
    for name in [m for m in sys.modules if m.startswith("invoicer")]:
        del sys.modules[name]
    from invoicer.parse import parse_amount
    from invoicer.totals import invoice_total

    def check(fn) -> bool:
        try:
            return bool(fn())
        except Exception:
            return False

    results = {
        # the bug model A diagnosed (visible failing test)
        "eu_dot_thousands": check(lambda: parse_amount("1.234,50") == Decimal("1234.50")
                                  and parse_amount("12.345.678,90") == Decimal("12345678.90")),
        # said only in chat, turn 1: the EU feed also uses a space as thousands separator
        "eu_space_thousands": check(lambda: parse_amount("1 234,50") == Decimal("1234.50")
                                    and parse_amount("1 234 567,89") == Decimal("1234567.89")),
        # existing behaviour kept
        "us_and_simple_kept": check(lambda: parse_amount("1,234.50") == Decimal("1234.50")
                                    and parse_amount("19,99") == Decimal("19.99")
                                    and parse_amount("19.99") == Decimal("19.99")),
        # said only in chat, turn 2: VAT rounded half-up per line, not on the total
        "vat_half_up_per_line": check(
            lambda: invoice_total([(1, "0.05"), (1, "0.05")], "0.10") == Decimal("0.12")
            and invoice_total([(1, "5.50")], "0.19") == Decimal("6.55")),
        # said only in chat, turn 3: signature unchanged
        "signature_unchanged": check(
            lambda: list(inspect.signature(invoice_total).parameters) == ["lines", "vat_rate"]),
    }
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                          "--collect-only", "-k", "test_regression_"],
                         cwd=root, capture_output=True, text=True)
    collected = sum(1 for line in run.stdout.splitlines() if "test_regression_" in line)
    suite = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                           cwd=root, capture_output=True, text=True)
    # said only in chat, turn 3: regression tests named test_regression_*, passing
    results["regression_tests_named"] = collected > 0 and suite.returncode == 0
    results["all_tests_pass"] = suite.returncode == 0
    print(json.dumps({"results": results, "score": f"{sum(results.values())}/{len(results)}",
                      "regression_tests": collected}, indent=2))


if __name__ == "__main__":
    main()
