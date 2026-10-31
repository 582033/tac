#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""技术指标模块测试。

同时支持两种运行方式::

    pytest tests/test_indicators.py
    python tests/test_indicators.py

测试只使用合成数据，不访问网络。
"""

from __future__ import annotations

import contextlib
import datetime
import json
import math
import os
import sys
import tempfile
import traceback

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import indicators as ind  # noqa: E402


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def assert_close(actual, expected, tol=1e-6):
    assert actual is not None, "期望 %r，实际为 None" % (expected,)
    assert abs(float(actual) - float(expected)) <= tol, "期望 %r，实际 %r（容差 %r）" % (
        expected,
        actual,
        tol,
    )


@contextlib.contextmanager
def assert_raises(exc_type):
    try:
        yield
    except exc_type:
        return
    raise AssertionError("未抛出 %s" % exc_type.__name__)


def make_rows(
    closes,
    highs=None,
    lows=None,
    volumes=None,
    start=datetime.date(2024, 1, 1),
):
    rows = []
    for i, close in enumerate(closes):
        day = start + datetime.timedelta(days=i)
        rows.append(
            {
                "date": day.isoformat(),
                "open": float(close),
                "high": float(highs[i]) if highs is not None else float(close) + 1.0,
                "low": float(lows[i]) if lows is not None else float(close) - 1.0,
                "close": float(close),
                "volume": float(volumes[i]) if volumes is not None else 1000.0,
                "amount": 0.0,
                "pct_chg": 0.0,
                "turnover": 0.0,
            }
        )
    return rows


def make_bundle(closes, highs=None, lows=None, volumes=None):
    return {"quotes": {"adjust": "qfq", "period": "daily", "rows": make_rows(closes, highs, lows, volumes)}}


def wavy_closes(n, base=100.0, amplitude=5.0, step=0.13):
    return [base + 0.5 * i + amplitude * math.sin(i * step) for i in range(n)]


# ---------------------------------------------------------------------------
# 基础指标：已知数值
# ---------------------------------------------------------------------------


def test_ma5_known_values():
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    out = ind.ma(series, 5)
    assert math.isnan(out.iloc[3]), "不足 5 日时 MA5 应为 NaN"
    assert_close(out.iloc[4], 3.0)  # (1+2+3+4+5)/5
    assert_close(out.iloc[5], 4.0)  # (2+3+4+5+6)/5


def test_ema_trend_and_warmup():
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    out = ind.ema(series, 3)
    assert math.isnan(out.iloc[1]), "不足 3 个观测时 EMA 应为 NaN"
    assert_close(out.iloc[2], 2.25)  # 1 -> 1.5 -> 2.25
    valid = out.dropna()
    assert valid.is_monotonic_increasing, "上涨序列的 EMA 应单调递增"
    assert_close(out.iloc[5], 5.03125)


def test_macd_constant_series_is_zero():
    series = pd.Series([100.0] * 60)
    frame = ind.macd(series, fast=12, slow=26, signal=9)
    assert math.isnan(frame["dif"].iloc[24]), "不足 26 行时 DIF 应为 NaN"
    assert not math.isnan(frame["dif"].iloc[25])
    for column in ("dif", "dea", "macd_hist"):
        assert_close(frame[column].iloc[-1], 0.0)


def test_rsi_bounds_and_extremes():
    up = pd.Series([100.0 + i for i in range(60)])
    down = pd.Series([200.0 - i for i in range(60)])
    flat = pd.Series([100.0] * 60)

    assert_close(ind.rsi(up, 14).iloc[-1], 100.0)
    assert_close(ind.rsi(down, 14).iloc[-1], 0.0)
    assert_close(ind.rsi(flat, 14).iloc[-1], 50.0)

    values = ind.rsi(pd.Series(wavy_closes(80)), 14).dropna()
    assert len(values) > 0
    assert float(values.min()) >= 0.0
    assert float(values.max()) <= 100.0


def test_boll_constant_and_ordering():
    flat = pd.Series([100.0] * 30)
    frame = ind.boll(flat, 20, 2.0)
    assert_close(frame["boll_upper"].iloc[-1], 100.0)
    assert_close(frame["boll_mid"].iloc[-1], 100.0)
    assert_close(frame["boll_lower"].iloc[-1], 100.0)

    frame2 = ind.boll(pd.Series(wavy_closes(80)), 20, 2.0)
    upper = frame2["boll_upper"].dropna()
    mid = frame2["boll_mid"].dropna()
    lower = frame2["boll_lower"].dropna()
    assert len(upper) == len(mid) == len(lower) == 61
    assert (upper >= mid - 1e-12).all()
    assert (mid >= lower - 1e-12).all()


def test_atr_known_value():
    high = pd.Series([110.0] * 30)
    low = pd.Series([90.0] * 30)
    close = pd.Series([100.0] * 30)
    out = ind.atr(high, low, close, 14)
    assert math.isnan(out.iloc[12])
    assert_close(out.iloc[13], 20.0)
    assert_close(out.iloc[-1], 20.0)


def test_kdj_known_recursion_and_bounds():
    closes = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10]
    high = pd.Series([10.0] * len(closes))
    low = pd.Series([0.0] * len(closes))
    close = pd.Series([float(c) for c in closes])
    frame = ind.kdj(high, low, close, 9, 3, 3)

    # 第 9 根 K 线开始有 RSV；首根 RSV = 9/10*100 = 90
    assert math.isnan(frame["kdj_k"].iloc[7])
    expected_k = (2.0 / 3.0) * 50.0 + (1.0 / 3.0) * 90.0
    expected_d = (2.0 / 3.0) * 50.0 + (1.0 / 3.0) * expected_k
    assert_close(frame["kdj_k"].iloc[8], expected_k)
    assert_close(frame["kdj_d"].iloc[8], expected_d)

    # 波动序列上 K/D 必须位于 [0, 100]
    wavy = wavy_closes(80)
    wavy_frame = ind.kdj(pd.Series(wavy), pd.Series(wavy), pd.Series(wavy), 9, 3, 3)
    for column in ("kdj_k", "kdj_d"):
        values = wavy_frame[column].dropna()
        assert len(values) > 0
        assert float(values.min()) >= 0.0
        assert float(values.max()) <= 100.0


def test_volume_ratio():
    assert ind.volume_ratio(pd.Series([100.0] * 10), 5, 20) is None, "不足 20 日应为 None"
    volumes = [100.0] * 15 + [200.0] * 5
    assert_close(ind.volume_ratio(pd.Series(volumes), 5, 20), 1.6)
    assert ind.volume_ratio(pd.Series([0.0] * 20), 5, 20) is None, "零均量应返回 None"


# ---------------------------------------------------------------------------
# 边界与数据校验
# ---------------------------------------------------------------------------


def test_direct_boundaries_small_samples():
    series = pd.Series([100.0 + i for i in range(10)])
    assert ind.ma(series, 60).isna().all()
    assert ind.ema(series, 26).isna().all()
    assert ind.boll(series, 20, 2.0).iloc[-1].isna().all()
    assert ind.rsi(series, 14).dropna().empty
    assert ind.volume_ratio(series, 5, 20) is None


def test_compute_technical_requires_30_rows():
    with assert_raises(ind.InsufficientDataError):
        ind.compute_technical(make_bundle([100.0] * 29))


def test_cli_exit_code_3_for_short_rows():
    with tempfile.TemporaryDirectory() as tmp:
        input_path = os.path.join(tmp, "bundle.json")
        with open(input_path, "w", encoding="utf-8") as handle:
            json.dump(make_bundle([100.0] * 29), handle)
        code = ind.main(["--input", input_path])
        assert code == 3
        assert not os.path.exists(os.path.join(tmp, "bundle.tech.json"))


def test_ma60_and_ema26_availability():
    closes = [100.0 + i for i in range(60)]
    tech60 = ind.compute_technical(make_bundle(closes))
    assert tech60["last"]["ma60"] is not None, "60 行时 ma60 应非空"
    assert tech60["last"]["ema26"] is not None, "60 行时 ema26 应非空"

    tech30 = ind.compute_technical(make_bundle(closes[:30]))
    assert tech30["last"]["ma60"] is None, "不足 60 行时 ma60 应为 null"
    assert tech30["last"]["ema26"] is None, "不足 60 行时 ema26 应为 null"
    assert tech30["last"]["ma20"] is not None, "30 行时 ma20 应可计算"


def test_technical_json_has_no_nan():
    bundle = make_bundle(wavy_closes(80))
    tech = ind.compute_technical(bundle)
    text = json.dumps(tech, ensure_ascii=False, allow_nan=False)
    assert "NaN" not in text
    assert "Infinity" not in text
    assert json.loads(text)["as_of"] is not None


def test_sanitize_json_replaces_non_finite():
    cleaned = ind.sanitize_json(
        {
            "a": float("nan"),
            "b": [float("inf"), 1.5],
            "c": {"d": float("-inf")},
            "e": "ok",
        }
    )
    assert cleaned == {"a": None, "b": [None, 1.5], "c": {"d": None}, "e": "ok"}


# ---------------------------------------------------------------------------
# series 对齐
# ---------------------------------------------------------------------------


def test_series_last_60_aligned():
    closes = wavy_closes(80)
    tech = ind.compute_technical(make_bundle(closes))
    series = tech["series"]
    for key in ("dates", "close", "ma20", "rsi14", "macd_hist"):
        assert len(series[key]) == 60, "%s 长度应为 60" % key
    assert all(isinstance(d, str) for d in series["dates"])
    expected_dates = [row["date"] for row in make_rows(closes)[-60:]]
    assert series["dates"] == expected_dates
    assert series["close"][-1] is not None
    assert tech["as_of"] == expected_dates[-1]


def test_series_null_alignment_for_short_history():
    tech = ind.compute_technical(make_bundle([100.0 + i for i in range(30)]))
    series = tech["series"]
    assert len(series["dates"]) == len(series["close"]) == len(series["ma20"]) == 30
    # 30 行时 MA20 从第 20 个位置开始有效，RSI14 从更早位置开始
    assert series["ma20"][:19] == [None] * 19
    assert series["ma20"][-1] is not None
    assert series["rsi14"][0] is None
    assert series["rsi14"][-1] is not None


# ---------------------------------------------------------------------------
# signals 分类
# ---------------------------------------------------------------------------


def test_signals_on_obvious_up_sequence():
    # 长期横盘后加速上涨：趋势、均线排列、RSI、MACD 方向均应一致
    closes = [100.0] * 60 + [100.0, 103.0, 108.0, 116.0, 128.0, 145.0]
    tech = ind.compute_technical(make_bundle(closes))
    signals = tech["signals"]
    assert signals["trend"] == "up"
    assert signals["ma_alignment"] == "bullish"
    assert signals["rsi_state"] == "overbought"
    assert signals["macd_state"] == "bullish"
    assert signals["volume_state"] == "normal"


def test_signals_on_obvious_down_sequence():
    closes = [100.0] * 60 + [100.0, 98.0, 94.0, 87.0, 77.0, 62.0]
    tech = ind.compute_technical(make_bundle(closes))
    signals = tech["signals"]
    assert signals["trend"] == "down"
    assert signals["ma_alignment"] == "bearish"
    assert signals["rsi_state"] == "oversold"
    assert signals["macd_state"] == "bearish"
    assert signals["volume_state"] == "normal"


def test_volume_states():
    closes = [100.0] * 60

    expanding = [1000.0] * 55 + [2000.0] * 5
    tech = ind.compute_technical(make_bundle(closes, volumes=expanding))
    assert_close(tech["last"]["volume_ratio"], 1.6)
    assert tech["signals"]["volume_state"] == "expanding"

    shrinking = [1000.0] * 55 + [500.0] * 5
    tech = ind.compute_technical(make_bundle(closes, volumes=shrinking))
    assert_close(tech["last"]["volume_ratio"], 0.571429, tol=1e-5)
    assert tech["signals"]["volume_state"] == "shrinking"

    normal = [1000.0] * 60
    tech = ind.compute_technical(make_bundle(closes, volumes=normal))
    assert_close(tech["last"]["volume_ratio"], 1.0)
    assert tech["signals"]["volume_state"] == "normal"


# ---------------------------------------------------------------------------
# CLI 输出
# ---------------------------------------------------------------------------


def test_default_output_path():
    assert ind.default_output_path("bundle.json") == "bundle.tech.json"
    assert ind.default_output_path("data/600519.json") == "data/600519.tech.json"
    assert ind.default_output_path("data/600519") == "data/600519.tech.json"


def test_cli_writes_default_output():
    closes = wavy_closes(80)
    with tempfile.TemporaryDirectory() as tmp:
        input_path = os.path.join(tmp, "bundle.json")
        with open(input_path, "w", encoding="utf-8") as handle:
            json.dump(make_bundle(closes), handle)

        code = ind.main(["--input", input_path])
        assert code == 0

        output_path = os.path.join(tmp, "bundle.tech.json")
        assert os.path.exists(output_path)
        with open(output_path, "r", encoding="utf-8") as handle:
            raw = handle.read()
        assert "NaN" not in raw
        assert "Infinity" not in raw

        data = json.loads(raw)
        assert "technical" in data
        assert data["technical"]["last"]["close"] == round(closes[-1], 6)
        assert len(data["technical"]["series"]["dates"]) == 60


def test_cli_window_limits_history():
    closes = [100.0 + i for i in range(80)]
    with tempfile.TemporaryDirectory() as tmp:
        input_path = os.path.join(tmp, "bundle.json")
        output_path = os.path.join(tmp, "custom.json")
        with open(input_path, "w", encoding="utf-8") as handle:
            json.dump(make_bundle(closes), handle)

        code = ind.main(["--input", input_path, "--out", output_path, "--window", "30"])
        assert code == 0
        with open(output_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        assert len(data["technical"]["series"]["dates"]) == 30
        assert data["technical"]["last"]["ma60"] is None


# ---------------------------------------------------------------------------
# 直接运行入口（无 pytest 时）
# ---------------------------------------------------------------------------


def _run_all():
    tests = sorted(
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    )
    failed = []
    for name, func in tests:
        try:
            func()
        except Exception:
            failed.append(name)
            print("FAIL %s" % name)
            traceback.print_exc()
        else:
            print("PASS %s" % name)

    print("\n%d passed, %d failed" % (len(tests) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
