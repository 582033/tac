#!/usr/bin/env python3
"""Tests for scripts/fetch_data.py.

Runnable both ways:
    pytest tests/test_fetch_data.py -q
    python tests/test_fetch_data.py

Every test is offline: network calls are replaced by tiny fake akshare
objects or by the committed fixture.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "sample_bundle.json"
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_data as fd  # noqa: E402


QUOTE_FIELDS = {
    "date", "open", "high", "low", "close", "volume", "amount",
    "pct_chg", "turnover",
}
BASIC_FIELDS = {
    "industry", "list_date", "is_st", "market_cap", "float_market_cap",
    "total_shares", "float_shares",
}
FUNDAMENTAL_FIELDS = {
    "period", "report_type", "roe", "gross_margin", "net_margin",
    "debt_ratio", "revenue", "net_profit", "ocf",
}
INCOME_FIELDS = {"period", "revenue", "net_profit", "operating_profit"}
BALANCE_FIELDS = {"period", "total_assets", "total_liabilities", "equity"}
CASHFLOW_FIELDS = {"period", "ocf", "icf", "fcf"}
NEWS_FIELDS = {"date", "title", "summary", "source", "url"}
VALUATION_FIELDS = {"pe_ttm", "pb", "ps_ttm", "dividend_yield", "total_mv", "circ_mv"}
SECTOR_FIELDS = {"name", "index_pct_chg", "rank", "rank_total", "main_net_inflow"}
MARKET_FIELDS = {"indexes", "up_count", "down_count", "northbound_net", "margin_balance"}
CONSTRAINT_FIELDS = {
    "suspended", "board", "price_limit_pct", "limit_up_price",
    "limit_down_price", "last_trade_date",
}

# Real Tencent qt.gtimg.cn payload for 600519 (fields after the 14-digit
# timestamp are the ones documented by the coordinator).
TENCENT_SNAPSHOT_RAW = (
    'v_sh600519="1~贵州茅台~600519~1392.05~1354.00~1363.00~59927~...~'
    '20261030161455~38.05~2.81~1393.37~1352.98~1392.05/59927/8257381778~'
    '59927~825738~0.48~21.18~~1393.37~1352.98~2.98~17401.76~17401.76~'
    '6.42~1489.40~1218.60~1.36~...~1250081601~1250081601~...";'
)


def _quote_row(close, volume=1000.0, trade_date="2026-10-30"):
    return {
        "date": trade_date,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
        "amount": close * volume,
        "pct_chg": 0.0,
        "turnover": 0.001,
    }


def _quotes(close, volume=1000.0):
    return {"adjust": "qfq", "period": "daily", "rows": [_quote_row(close, volume)]}


def _numeric_or_null(value):
    assert value is None or (
        isinstance(value, (int, float)) and not isinstance(value, bool)
    ), "expected number or null, got %r" % (value,)


def _walk_numeric(value, path="$"):
    if isinstance(value, dict):
        for key, item in value.items():
            _walk_numeric(item, "%s.%s" % (path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk_numeric(item, "%s[%d]" % (path, index))
    elif isinstance(value, float):
        assert value == value, "NaN at %s" % path
        assert abs(value) != float("inf"), "Inf at %s" % path
    elif isinstance(value, int) and not isinstance(value, bool):
        pass


# ---------------------------------------------------------------------------
# 1. Symbol normalization
# ---------------------------------------------------------------------------


def test_resolve_symbol_normalization():
    cases = {
        "600519.SH": ("600519", "SH", "main", "sh600519"),
        "600519": ("600519", "SH", "main", "sh600519"),
        "SH600519": ("600519", "SH", "main", "sh600519"),
        "000001": ("000001", "SZ", "main", "sz000001"),
        "300750": ("300750", "SZ", "chinext", "sz300750"),
        "830799": ("830799", "BJ", "bse", "bj830799"),
        "688981": ("688981", "SH", "star", "sh688981"),
    }
    for raw, expected in cases.items():
        info = fd.resolve_symbol(raw)
        code, exchange, board, akshare_symbol = expected
        assert info["code"] == code, (raw, info)
        assert info["symbol"] == code
        assert info["exchange"] == exchange
        assert info["board"] == board
        assert info["akshare_symbol"] == akshare_symbol
        assert info["akshare_prefix"] == akshare_symbol
        assert info["full_symbol"] == "%s.%s" % (code, exchange)
        assert info["ts_code"] == "%s.%s" % (code, exchange)

    try:
        fd.resolve_symbol("not-a-code")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid symbol should raise ValueError")


# ---------------------------------------------------------------------------
# 2. Offline mode output conforms to the CONTRACT schema
# ---------------------------------------------------------------------------


def test_offline_output_matches_schema():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "bundle.json"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = fd.main([
                "--symbol", "600519",
                "--offline", str(FIXTURE),
                "--out", str(out),
            ])
        assert rc == 0, "offline fixture should be complete"
        assert "WROTE " in buffer.getvalue()
        data = json.loads(out.read_text(encoding="utf-8"))

    # Top-level contract sections.
    for key in (
        "schema_version", "meta", "basic", "quotes", "valuation",
        "fundamentals", "financials", "news", "sector", "market",
        "constraints", "technical",
    ):
        assert key in data, "missing top-level key: %s" % key
    assert data["schema_version"] == "1.0"

    meta = data["meta"]
    assert meta["symbol"] == "600519"
    assert meta["name"] == "贵州茅台"
    assert meta["exchange"] == "SH"
    assert meta["as_of"] == "2026-10-31"
    assert meta["currency"] == "CNY"
    assert meta["source"] == "fixture"
    assert isinstance(meta["warnings"], list)
    fetched_at = datetime.fromisoformat(meta["fetched_at"])
    assert fetched_at.tzinfo is not None

    assert set(data["basic"]) == BASIC_FIELDS
    assert data["basic"]["is_st"] is False

    quotes = data["quotes"]
    assert quotes["adjust"] == "qfq"
    assert quotes["period"] == "daily"
    rows = quotes["rows"]
    assert len(rows) >= 120
    assert set(rows[0]) == QUOTE_FIELDS
    dates = [date.fromisoformat(row["date"]) for row in rows]
    assert dates == sorted(dates)
    assert all(day.weekday() < 5 for day in dates), "weekend bars are not allowed"
    for previous, current in zip(dates, dates[1:]):
        expected = 3 if previous.weekday() == 4 else 1
        assert (current - previous).days == expected, "fixture dates must skip weekends only"
    assert rows[-1]["date"] <= meta["as_of"]

    assert set(data["valuation"]) == VALUATION_FIELDS

    periods = data["fundamentals"]["periods"]
    assert len(periods) == 8
    assert periods[0]["period"] == "2026-09-30"
    for period in periods:
        assert set(period) == FUNDAMENTAL_FIELDS
    for section, fields in (
        ("income", INCOME_FIELDS),
        ("balance", BALANCE_FIELDS),
        ("cashflow", CASHFLOW_FIELDS),
    ):
        rows_fin = data["financials"][section]
        assert len(rows_fin) == 8
        assert [row["period"] for row in rows_fin] == [p["period"] for p in periods]
        for row in rows_fin:
            assert set(row) == fields

    assert len(data["news"]) == 12
    for item in data["news"]:
        assert set(item) == NEWS_FIELDS
        assert item["date"] <= meta["as_of"]

    assert set(data["sector"]) == SECTOR_FIELDS
    assert set(data["market"]) == MARKET_FIELDS
    for item in data["market"]["indexes"]:
        assert set(item) == {"name", "close", "pct_chg"}

    constraints = data["constraints"]
    assert set(constraints) == CONSTRAINT_FIELDS
    assert constraints["board"] == "main"
    assert constraints["price_limit_pct"] == 0.1
    assert constraints["last_trade_date"] == rows[-1]["date"]
    assert constraints["limit_up_price"] == fd._round_half_up(rows[-1]["close"] * 1.1, 2)
    assert constraints["limit_down_price"] == fd._round_half_up(rows[-1]["close"] * 0.9, 2)

    # No NaN/Inf anywhere.
    _walk_numeric(data)


# ---------------------------------------------------------------------------
# 3. Missing values are null, never 0
# ---------------------------------------------------------------------------


class FakeFinanceAK:
    """Minimal akshare stand-in with a deliberately incomplete quarter."""

    def stock_profit_sheet_by_report_em(self, symbol=None):
        return [{
            "REPORT_DATE": "2026-06-30",
            "NOTICE_DATE": "2026-08-25",
            "TOTAL_OPERATE_INCOME": 1000.0,
            "PARENT_NETPROFIT": None,
            "OPERATE_PROFIT": None,
            "OPERATE_COST": 300.0,
        }]

    def stock_balance_sheet_by_report_em(self, symbol=None):
        return [{
            "REPORT_DATE": "2026-06-30",
            "NOTICE_DATE": "2026-08-25",
            "TOTAL_ASSETS": 5000.0,
            "TOTAL_LIABILITIES": 1000.0,
            "TOTAL_PARENT_EQUITY": None,
        }]

    def stock_cash_flow_sheet_by_report_em(self, symbol=None):
        return [{
            "REPORT_DATE": "2026-06-30",
            "NOTICE_DATE": "2026-08-25",
            "NETCASH_OPERATE": None,
            "NETCASH_INVEST": None,
            "FREE_CASH_FLOW": None,
        }]


def test_missing_fields_are_null_not_zero():
    assert fd._safe_float(None) is None
    assert fd._safe_float("") is None
    assert fd._safe_float("--") is None
    assert fd._safe_float("nan") is None
    assert fd._safe_float(0) == 0.0  # a genuine zero stays zero

    warnings = []
    result = fd.fetch_financials(
        "600519", as_of="2026-10-31", warnings=warnings,
        ak_module=FakeFinanceAK(),
    )
    period = result["fundamentals"]["periods"][0]
    income = result["financials"]["income"][0]
    cashflow = result["financials"]["cashflow"][0]

    assert period["period"] == "2026-06-30"
    assert period["revenue"] == 1000.0
    assert period["net_profit"] is None
    assert period["net_profit"] != 0
    assert period["roe"] is None
    assert period["ocf"] is None
    assert income["net_profit"] is None
    assert cashflow["ocf"] is None
    assert cashflow["fcf"] is None
    assert any("finance" in item and "字段缺失" in item for item in warnings)


class FakeWideAbstractAK:
    """Mimics akshare 1.18.88 ``stock_financial_abstract`` wide output."""

    def stock_financial_abstract(self, symbol=None):
        def row(name, q3, q2, future):
            return {
                "选项": "常用指标",
                "指标": name,
                "20260930": q3,
                "20260630": q2,
                "20261231": future,  # must be filtered out by as_of
            }

        return [
            row("营业总收入", 139_200_000_000, 98_100_000_000, 1.0),
            row("归母净利润", 75_200_000_000, 50_900_000_000, 1.0),
            row("营业利润", 100_000_000_000, 67_800_000_000, 1.0),
            row("营业成本", 11_400_000_000, 7_800_000_000, 1.0),
            row("净资产收益率", 22.5, 15.4, 1.0),
            row("销售毛利率", 91.8, 92.0, 1.0),
            row("销售净利率", 54.0, 51.9, 1.0),
            row("资产负债率", 19.1, 18.9, 1.0),
            row("经营现金流量净额", 55_100_000_000, 31_800_000_000, 1.0),
            row("投资现金流量净额", -8_800_000_000, -5_200_000_000, 1.0),
            row("总资产", 298_000_000_000, 285_000_000_000, 1.0),
            row("总负债", 57_000_000_000, 54_000_000_000, 1.0),
            row("股东权益合计", 241_000_000_000, 231_000_000_000, 1.0),
        ]


def test_financial_abstract_wide_table_mapping():
    warnings = []
    result = fd.fetch_financials(
        "600519", as_of="2026-10-31", warnings=warnings,
        ak_module=FakeWideAbstractAK(),
    )
    periods = result["fundamentals"]["periods"]
    assert [p["period"] for p in periods] == ["2026-09-30", "2026-06-30"]
    latest = periods[0]
    assert latest["report_type"] == "quarter"
    assert latest["revenue"] == 139_200_000_000.0
    assert latest["net_profit"] == 75_200_000_000.0
    assert latest["roe"] == 0.225
    assert latest["gross_margin"] == 0.918
    assert latest["net_margin"] == 0.54
    assert latest["debt_ratio"] == 0.191
    assert latest["ocf"] == 55_100_000_000.0

    assert result["financials"]["income"][0]["operating_profit"] == 100_000_000_000.0
    assert result["financials"]["balance"][0]["total_assets"] == 298_000_000_000.0
    assert result["financials"]["cashflow"][0]["icf"] == -8_800_000_000.0
    assert result["financials"]["cashflow"][0]["fcf"] is None
    assert any("披露日期" in item for item in warnings)


class FakeValuationAK:
    def stock_zh_a_spot_em(self):
        return [{
            "代码": "600519",
            "名称": "贵州茅台",
            "市盈率-动态": 24.6,
            "市净率": 8.1,
            "总市值": 2_383_622_763_522.0,
            "流通市值": 2_383_622_763_522.0,
        }]


def test_valuation_uses_spot_snapshot():
    warnings = []
    valuation = fd.fetch_valuation(
        "600519", basic=None, warnings=warnings,
        ak_module=FakeValuationAK(), tencent_fetcher=lambda: None,
    )
    assert valuation["pe_ttm"] == 24.6
    assert valuation["pb"] == 8.1
    assert valuation["total_mv"] == 2_383_622_763_522.0
    assert valuation["ps_ttm"] is None
    assert valuation["dividend_yield"] is None
    assert any("动态市盈率" in item for item in warnings)
    assert any("字段缺失" in item for item in warnings)


# ---------------------------------------------------------------------------
# 3b. Tencent fallback (offline parser + integration)
# ---------------------------------------------------------------------------


class EmptyAK:
    """An akshare stand-in where every Eastmoney endpoint is unavailable."""


def test_parse_tencent_snapshot_offline():
    snapshot = fd.parse_tencent_snapshot(TENCENT_SNAPSHOT_RAW)
    assert snapshot["name"] == "贵州茅台"
    assert snapshot["code"] == "600519"
    assert snapshot["price"] == 1392.05
    assert abs(snapshot["pe_ttm"] - 21.18) < 1e-9
    assert abs(snapshot["pb"] - 6.42) < 1e-9
    assert abs(snapshot["market_cap"] - 17401.76 * 1e8) < 1.0
    assert abs(snapshot["float_market_cap"] - 17401.76 * 1e8) < 1.0
    assert snapshot["limit_up_price"] == 1489.40
    assert snapshot["limit_down_price"] == 1218.60
    assert snapshot["total_shares"] == 1250081601.0
    assert snapshot["float_shares"] == 1250081601.0
    assert snapshot["trade_date"] == "2026-10-30"


def test_tencent_fills_basic_valuation_and_constraints():
    snapshot = fd.parse_tencent_snapshot(TENCENT_SNAPSHOT_RAW)

    warnings = []
    basic = fd.fetch_basic(
        "600519", warnings=warnings, ak_module=EmptyAK(),
        tencent_fetcher=lambda: snapshot,
    )
    assert basic["name"] == "贵州茅台"
    assert basic["market_cap"] == 1740176000000.0
    assert basic["float_market_cap"] == 1740176000000.0
    assert basic["total_shares"] == 1250081601.0
    assert basic["float_shares"] == 1250081601.0
    assert any("腾讯行情快照" in item for item in warnings)

    warnings = []
    valuation = fd.fetch_valuation(
        "600519", basic=basic, warnings=warnings, ak_module=EmptyAK(),
        as_of="2026-10-31", tencent_fetcher=lambda: snapshot,
    )
    assert valuation["pe_ttm"] == 21.18
    assert valuation["pb"] == 6.42
    assert valuation["total_mv"] == 1740176000000.0
    assert valuation["circ_mv"] == 1740176000000.0
    assert valuation["ps_ttm"] is None
    assert valuation["dividend_yield"] is None
    assert any("腾讯行情快照" in item for item in warnings)

    warnings = []
    constraints = fd.fetch_constraints(
        _quotes(100.0), {"is_st": False}, symbol="600519",
        warnings=warnings, as_of="2026-10-31", tencent_fetcher=lambda: snapshot,
    )
    assert constraints["limit_up_price"] == 1489.40
    assert constraints["limit_down_price"] == 1218.60
    assert any("腾讯行情快照" in item for item in warnings)


def test_tencent_kline_quote_fallback():
    records = [
        {
            "日期": "2026-10-28", "开盘": 100.0, "收盘": 101.0,
            "最高": 102.0, "最低": 99.5, "成交量": 1200,
        },
        {
            "日期": "2026-10-29", "开盘": 101.0, "收盘": 102.0,
            "最高": 102.5, "最低": 100.5, "成交量": 1500,
        },
    ]
    warnings = []
    result = fd.fetch_quotes(
        "600519", as_of="2026-10-31", warnings=warnings,
        ak_module=EmptyAK(), tencent_kline_fetcher=lambda: records,
    )
    assert result is not None
    rows = result["rows"]
    assert [row["date"] for row in rows] == ["2026-10-28", "2026-10-29"]
    assert rows[0]["amount"] is None
    assert rows[0]["turnover"] is None
    assert rows[1]["pct_chg"] == round((102.0 - 101.0) / 101.0, 6)
    assert any("成交额缺失" in item for item in warnings)


# ---------------------------------------------------------------------------
# 3c. Northbound: missing/placeholder zero must stay null
# ---------------------------------------------------------------------------


class FakeNorthboundZeroAK:
    def stock_hsgt_fund_flow_summary_em(self):
        return [
            {"类型": "沪股通", "资金方向": "北向", "成交净买额": 0.0},
            {"类型": "深股通", "资金方向": "北向", "成交净买额": 0.0},
        ]


class FakeNorthboundMixedZeroAK:
    def stock_hsgt_fund_flow_summary_em(self):
        return [
            {"类型": "沪股通", "资金方向": "北向", "成交净买额": 5.0},
            {"类型": "深股通", "资金方向": "北向", "成交净买额": -5.0},
        ]


def test_northbound_all_zero_placeholder_stays_null():
    warnings = []
    market = fd.fetch_market(
        as_of="2026-10-31", warnings=warnings, ak_module=FakeNorthboundZeroAK(),
    )
    assert market["northbound_net"] is None
    assert any("北向资金接口仅返回 0" in item for item in warnings)


def test_northbound_explicit_zero_after_offset():
    warnings = []
    market = fd.fetch_market(
        as_of="2026-10-31", warnings=warnings, ak_module=FakeNorthboundMixedZeroAK(),
    )
    assert market["northbound_net"] == 0.0
    assert any("多空抵消" in item for item in warnings)


# ---------------------------------------------------------------------------
# 4. Partial failures become warnings, not exceptions
# ---------------------------------------------------------------------------


class FailingQuotesAK:
    def stock_zh_a_hist(self, **kwargs):
        raise RuntimeError("boom")  # non-retryable, keeps the test fast


def test_partial_failure_writes_warnings():
    warnings = []
    result = fd.fetch_quotes(
        "600519", as_of="2026-10-31", warnings=warnings,
        ak_module=FailingQuotesAK(), tencent_kline_fetcher=lambda: None,
    )
    assert result is None
    assert any(item.startswith("quotes:") for item in warnings), warnings
    assert "stock_zh_a_hist" in " ".join(warnings)

    # safe_call is the isolation boundary used by every module.
    warnings = []

    def broken():
        raise RuntimeError("akshare 接口超时")

    value = fd.safe_call("valuation", broken, warnings, default=None)
    assert value is None
    assert any(item.startswith("valuation:") for item in warnings), warnings


def test_collect_bundle_keeps_going_after_one_module_fails():
    warnings = []
    row = _quote_row(100.0, volume=2000.0)
    fetchers = {
        "quotes": lambda: {"adjust": "qfq", "period": "daily", "rows": [row]},
        "basic": lambda: {
            "name": "测试股份", "industry": "白酒", "list_date": "2001-08-27",
            "is_st": False, "market_cap": 1e9, "float_market_cap": 1e9,
            "total_shares": 1e8, "float_shares": 1e8,
        },
        "valuation": lambda: (_ for _ in ()).throw(RuntimeError("akshare 接口超时")),
        "finance": lambda: fd.empty_financials(),
        "news": lambda: [],
        "sector": lambda: fd.empty_sector(),
        "market": lambda: fd.empty_market(),
        "constraints": lambda: fd.fetch_constraints(
            {"rows": [row]}, {"is_st": False}, symbol="600519",
            warnings=[], as_of="2026-10-31", tencent_fetcher=lambda: None),
    }
    bundle = fd.collect_bundle(
        "600519", as_of="2026-10-31", warnings=warnings, fetchers=fetchers,
    )
    assert any(item.startswith("valuation:") for item in warnings), warnings
    assert bundle["meta"]["warnings"] == warnings
    assert bundle["meta"]["name"] == "测试股份"
    assert bundle["quotes"]["rows"][0]["close"] == 100.0
    assert any(item.startswith("valuation:") for item in bundle["meta"]["warnings"])


# ---------------------------------------------------------------------------
# 5. Price limit / board rules
# ---------------------------------------------------------------------------


def test_price_limits_by_board_and_st():
    cases = [
        ("600519", False, "main", 0.1, 110.0, 90.0),
        ("688981", False, "star", 0.2, 120.0, 80.0),
        ("300750", False, "chinext", 0.2, 120.0, 80.0),
        ("830799", False, "bse", 0.3, 130.0, 70.0),
        ("600519", True, "main", 0.05, 105.0, 95.0),
    ]
    for code, is_st, board, limit_pct, limit_up, limit_down in cases:
        warnings = []
        result = fd.fetch_constraints(
            _quotes(100.0), {"is_st": is_st}, symbol=code,
            warnings=warnings, as_of="2026-10-31", tencent_fetcher=lambda: None,
        )
        assert result["board"] == board, code
        assert result["price_limit_pct"] == limit_pct, code
        assert result["limit_up_price"] == limit_up, (code, result)
        assert result["limit_down_price"] == limit_down, (code, result)
        assert result["last_trade_date"] == "2026-10-30"
        assert result["suspended"] is False


def test_price_limit_rounding_and_suspension():
    # 10.05 * 1.10 = 11.055 -> 11.06 (half-up), 10.05 * 0.90 = 9.045 -> 9.05.
    warnings = []
    result = fd.fetch_constraints(
        _quotes(10.05), {"is_st": False}, symbol="600519",
        warnings=warnings, as_of="2026-10-31", tencent_fetcher=lambda: None,
    )
    assert result["limit_up_price"] == 11.06
    assert result["limit_down_price"] == 9.05

    warnings = []
    result = fd.fetch_constraints(
        _quotes(10.0, volume=0.0), {"is_st": False}, symbol="600519",
        warnings=warnings, as_of="2026-10-31", tencent_fetcher=lambda: None,
    )
    assert result["suspended"] is True
    assert any("停牌" in item for item in warnings)


# ---------------------------------------------------------------------------
# 6. Quote normalization: qfq fields, percent -> decimal, keep zero-volume bars
# ---------------------------------------------------------------------------


class FakeQuotesAK:
    def stock_zh_a_hist(self, **kwargs):
        return [
            {
                "日期": "2026-10-28", "开盘": 100.0, "收盘": 101.0,
                "最高": 102.0, "最低": 99.5, "成交量": 1200,
                "成交额": 12120000.0, "涨跌幅": 1.5, "换手率": 0.8,
            },
            {
                "日期": "2026-10-29", "开盘": 101.0, "收盘": 101.0,
                "最高": 101.5, "最低": 100.5, "成交量": 0,
                "成交额": 0.0, "涨跌幅": 0.0, "换手率": 0.0,
            },
            {
                "日期": "2026-10-30", "开盘": 0.0, "收盘": 0.0,
                "最高": 0.0, "最低": 0.0, "成交量": 0,
                "成交额": 0.0, "涨跌幅": 0.0, "换手率": 0.0,
            },
        ]


def test_quote_normalization_and_zero_volume_warning():
    warnings = []
    result = fd.fetch_quotes(
        "600519", as_of="2026-10-31", days=400, warnings=warnings,
        ak_module=FakeQuotesAK(), tencent_kline_fetcher=lambda: None,
    )
    assert result is not None
    assert result["adjust"] == "qfq"
    assert result["period"] == "daily"
    rows = result["rows"]
    assert [row["date"] for row in rows] == ["2026-10-28", "2026-10-29"]
    assert set(rows[0]) == QUOTE_FIELDS
    assert rows[0]["pct_chg"] == 0.015
    assert rows[0]["turnover"] == 0.008
    assert rows[1]["volume"] == 0.0
    assert any("成交量为 0" in item for item in warnings)
    assert any("剔除" in item for item in warnings)
    # Only two bars -> a warning, but not a fatal error.
    assert any("少于 30" in item for item in warnings)


# ---------------------------------------------------------------------------
# Direct execution: python tests/test_fetch_data.py
# ---------------------------------------------------------------------------


def _run_all():
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failed = 0
    for name, func in tests:
        try:
            func()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print("FAIL %s: %s" % (name, exc))
            traceback.print_exc()
        else:
            print("PASS %s" % name)
    passed = len(tests) - failed
    print("\n%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
