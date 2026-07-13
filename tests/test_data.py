"""Currency-normalisation tests. These exist because of a live bug:
"GBp".lower() == "GBP".lower(), so pence quotes were once treated as
pounds and every LSE price came through 100x too high."""

import pandas as pd
import pytest

from momo.data import to_gbp

IDX = pd.to_datetime(["2026-07-09", "2026-07-10"])


def frame(**cols):
    return pd.DataFrame(cols, index=IDX)


def test_uk_default_is_pence_divided_by_100():
    out = to_gbp(frame(**{"GLEN.L": [510.0, 514.5]}), {"GLEN.L": "UK"}, 1.25, {})
    assert out["GLEN.L"].tolist() == pytest.approx([5.10, 5.145])


def test_us_default_divided_by_fx():
    out = to_gbp(frame(NVDA=[125.0, 130.0]), {"NVDA": "US"}, 1.25, {})
    assert out["NVDA"].tolist() == [100.0, 104.0]


def test_verified_gbp_quote_passes_through_unchanged():
    # a .L name genuinely quoted in pounds must NOT be divided by 100
    out = to_gbp(frame(**{"ODD.L": [12.5, 13.0]}), {"ODD.L": "UK"}, 1.25,
                 {"ODD.L": "GBP"})
    assert out["ODD.L"].tolist() == [12.5, 13.0]


def test_verified_gbp_vs_pence_are_distinct():
    # the exact collision that shipped: GBp (pence) and GBP (pounds)
    # differ only by case and must behave differently
    f = frame(**{"A.L": [200.0, 200.0], "B.L": [200.0, 200.0]})
    out = to_gbp(f, {"A.L": "UK", "B.L": "UK"}, 1.25,
                 {"A.L": "GBp", "B.L": "GBP"})
    assert out["A.L"].iloc[0] == pytest.approx(2.0)
    assert out["B.L"].iloc[0] == pytest.approx(200.0)


def test_gbx_alias_treated_as_pence():
    out = to_gbp(frame(**{"C.L": [150.0, 150.0]}), {"C.L": "UK"}, 1.25,
                 {"C.L": "GBX"})
    assert out["C.L"].iloc[0] == pytest.approx(1.5)


def test_fx_series_applied_row_wise():
    fx = pd.Series([1.25, 1.30], index=IDX)
    out = to_gbp(frame(NVDA=[125.0, 130.0]), {"NVDA": "US"}, fx, {})
    assert out["NVDA"].tolist() == [100.0, 100.0]


def test_unknown_currency_left_unconverted():
    out = to_gbp(frame(**{"E.L": [9.0, 9.0]}), {"E.L": "UK"}, 1.25,
                 {"E.L": "EUR"})
    assert out["E.L"].iloc[0] == 9.0
