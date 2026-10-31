#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A 股技术指标计算模块（纯离线计算）。

读取 fetch_data.py 产出的 bundle JSON，计算技术指标后写回
``bundle["technical"]``，并输出到 ``*.tech.json``。

算法参考自 TradingAgents-CN 的 ``tools/analysis/indicators.py``
（Apache-2.0），本文件为独立可运行版本，不依赖原项目任何模块。

命令行::

    python scripts/indicators.py --input bundle.json [--out PATH] [--window 400]

退出码：0 成功；3 致命错误（行情不足或输入不可用）。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

MIN_ROWS = 30
SERIES_LENGTH = 60
ROUND_DIGITS = 6


class InsufficientDataError(ValueError):
    """行情行数不足，无法计算技术指标。"""


# ---------------------------------------------------------------------------
# 基础指标（与蓝本保持一致的算法）
# ---------------------------------------------------------------------------


def _as_series(values: Sequence[Any]) -> pd.Series:
    """把任意序列安全转换为 float Series，非法值记为 NaN。"""
    return pd.Series(pd.to_numeric(pd.Series(list(values)), errors="coerce"), dtype="float64")


def ma(close: pd.Series, n: int, min_periods: Optional[int] = None) -> pd.Series:
    """简单移动平均。默认要求完整窗口，不足 n 个有效值时返回 NaN。"""
    n = int(n)
    if min_periods is None:
        min_periods = n
    return close.rolling(window=n, min_periods=int(min_periods)).mean()


def ema(close: pd.Series, n: int) -> pd.Series:
    """指数移动平均。不足 n 个有效值时为 NaN。"""
    n = int(n)
    return close.ewm(span=n, adjust=False, min_periods=n).mean()


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """MACD(12, 26, 9)。返回 dif / dea / macd_hist 三列。"""
    dif = ema(close, fast) - ema(close, slow)
    dea = dif.ewm(span=int(signal), adjust=False, min_periods=1).mean()
    hist = dif - dea
    return pd.DataFrame({"dif": dif, "dea": dea, "macd_hist": hist})


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """RSI（Wilder 平滑，国际标准），取值 [0, 100]。

    - 全涨 -> 100
    - 全跌 -> 0
    - 完全无波动 -> 50
    - 不足 n+1 个价格 -> NaN
    """
    n = int(n)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = gain.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()

    denom = avg_gain + avg_loss
    out = pd.Series(np.nan, index=close.index, dtype="float64")
    rising = denom.notna() & (denom > 0)
    flat = denom.notna() & (denom == 0)
    out.loc[rising] = 100.0 * avg_gain.loc[rising] / denom.loc[rising]
    out.loc[flat] = 50.0
    return out.clip(lower=0.0, upper=100.0)


def boll(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    """布林带（20, 2）。返回 boll_mid / boll_upper / boll_lower。"""
    n = int(n)
    mid = close.rolling(window=n, min_periods=n).mean()
    std = close.rolling(window=n, min_periods=n).std(ddof=1)
    upper = mid + float(k) * std
    lower = mid - float(k) * std
    return pd.DataFrame({"boll_mid": mid, "boll_upper": upper, "boll_lower": lower})


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """ATR（真实波幅的 n 日简单平均）。"""
    n = int(n)
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window=n, min_periods=n).mean()


def kdj(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    n: int = 9,
    m1: int = 3,
    m2: int = 3,
) -> pd.DataFrame:
    """KDJ(9, 3, 3)。K/D 初始化为 50，按经典递推公式计算。"""
    n, m1, m2 = int(n), int(m1), int(m2)
    lowest_low = low.rolling(window=n, min_periods=n).min()
    highest_high = high.rolling(window=n, min_periods=n).max()
    span = highest_high - lowest_low

    rsv = pd.Series(np.nan, index=close.index, dtype="float64")
    normal = span.notna() & (span > 0)
    flat = span.notna() & (span == 0)
    rsv.loc[normal] = (close.loc[normal] - lowest_low.loc[normal]) / span.loc[normal] * 100.0
    rsv.loc[flat] = 50.0
    rsv = rsv.clip(lower=0.0, upper=100.0)

    k = pd.Series(np.nan, index=close.index, dtype="float64")
    d = pd.Series(np.nan, index=close.index, dtype="float64")
    alpha_k = 1.0 / float(m1)
    alpha_d = 1.0 / float(m2)
    last_k = 50.0
    last_d = 50.0

    for i in range(len(close)):
        rv = rsv.iloc[i]
        if pd.isna(rv):
            continue
        curr_k = (1.0 - alpha_k) * last_k + alpha_k * float(rv)
        curr_d = (1.0 - alpha_d) * last_d + alpha_d * curr_k
        k.iloc[i] = curr_k
        d.iloc[i] = curr_d
        last_k, last_d = curr_k, curr_d

    j = 3.0 * k - 2.0 * d
    return pd.DataFrame({"kdj_k": k, "kdj_d": d, "kdj_j": j})


def volume_ratio(volume: pd.Series, short: int = 5, long: int = 20) -> Optional[float]:
    """近 5 日均量 / 近 20 日均量。数据不足或均量为 0 时返回 None。"""
    short, long = int(short), int(long)
    if len(volume) < long:
        return None
    short_mean = volume.tail(short).mean()
    long_mean = volume.tail(long).mean()
    if pd.isna(short_mean) or pd.isna(long_mean) or float(long_mean) <= 0:
        return None
    return float(short_mean) / float(long_mean)


# ---------------------------------------------------------------------------
# 序列与 JSON 安全处理
# ---------------------------------------------------------------------------


def _finite_or_none(value: Any, digits: Optional[int] = ROUND_DIGITS) -> Optional[float]:
    """把任意数值转为有限 float；NaN/inf/非法值统一返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if digits is not None:
        number = round(number, digits)
    return number


def _last_valid(series: pd.Series) -> Optional[float]:
    """返回序列中最后一个有效（有限）值。"""
    if series is None or len(series) == 0:
        return None
    cleaned = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if cleaned.empty:
        return None
    return _finite_or_none(cleaned.iloc[-1])


def _last_valid_values(series: pd.Series, count: int) -> List[float]:
    cleaned = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if cleaned.empty:
        return []
    return [float(v) for v in cleaned.tail(int(count)).tolist()]


def _date_string(value: Any) -> str:
    """日期统一转成字符串（None 用空串，保证 series.dates 全为 str）。"""
    if value is None:
        return ""
    return str(value)


def sanitize_json(value: Any) -> Any:
    """递归清理 JSON 数据：非有限浮点数 -> None，numpy 标量 -> Python 标量。"""
    if isinstance(value, dict):
        return {str(k): sanitize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return _finite_or_none(value, digits=None)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return _finite_or_none(number, digits=None)


# ---------------------------------------------------------------------------
# bundle -> technical
# ---------------------------------------------------------------------------


def _extract_rows(bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
    quotes = bundle.get("quotes") if isinstance(bundle, dict) else None
    rows = quotes.get("rows") if isinstance(quotes, dict) else None
    if not isinstance(rows, list):
        raise InsufficientDataError("bundle.quotes.rows 缺失或不是数组，无法计算技术指标")
    if len(rows) < MIN_ROWS:
        raise InsufficientDataError(
            "quotes.rows 仅 %d 行，至少需要 %d 行（不足则退出码 3）"
            % (len(rows), MIN_ROWS)
        )
    return [r for r in rows if isinstance(r, dict)]


def _sort_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按日期字符串升序排序；日期缺失时保持原顺序。"""
    dates = [r.get("date") for r in rows]
    if all(isinstance(d, str) and d for d in dates):
        return [r for _, r in sorted(zip(dates, rows), key=lambda pair: pair[0])]
    return rows


def _column(rows: List[Dict[str, Any]], key: str) -> pd.Series:
    return _as_series([row.get(key) for row in rows])


def _classify_trend(
    close: Optional[float],
    ma20: Optional[float],
    ma60: Optional[float],
    ma20_slope: Optional[float],
) -> str:
    """用收盘价/MA20/MA60 关系与 MA20 近 5 日斜率投票。"""
    if close is None or ma20 is None:
        return "unknown"

    score = 0
    if close > ma20:
        score += 1
    elif close < ma20:
        score -= 1

    if ma60 is not None:
        if ma20 > ma60:
            score += 1
        elif ma20 < ma60:
            score -= 1

    if ma20_slope is not None:
        if ma20_slope > 0:
            score += 1
        elif ma20_slope < 0:
            score -= 1

    if score >= 2:
        return "up"
    if score <= -2:
        return "down"
    return "sideways"


def _classify_ma_alignment(
    ma5: Optional[float],
    ma10: Optional[float],
    ma20: Optional[float],
    ma60: Optional[float],
) -> str:
    values = (ma5, ma10, ma20, ma60)
    if any(v is None for v in values):
        return "unknown"
    if ma5 > ma10 > ma20 > ma60:
        return "bullish"
    if ma5 < ma10 < ma20 < ma60:
        return "bearish"
    return "mixed"


def _classify_rsi(rsi14: Optional[float]) -> str:
    if rsi14 is None:
        return "unknown"
    if rsi14 > 70:
        return "overbought"
    if rsi14 < 30:
        return "oversold"
    return "neutral"


def _classify_macd(hist_values: List[float]) -> str:
    if len(hist_values) < 2:
        return "unknown"
    prev_hist, last_hist = hist_values[-2], hist_values[-1]
    if last_hist > 0 and last_hist > prev_hist:
        return "bullish"
    if last_hist < 0 and last_hist < prev_hist:
        return "bearish"
    return "neutral"


def _classify_volume(ratio: Optional[float]) -> str:
    if ratio is None:
        return "unknown"
    if ratio > 1.2:
        return "expanding"
    if ratio < 0.8:
        return "shrinking"
    return "normal"


def compute_technical(bundle: Dict[str, Any], window: int = 400) -> Dict[str, Any]:
    """计算完整的 ``technical`` 段。输入为 bundle dict，返回新 dict。"""
    rows = _extract_rows(bundle)
    rows = _sort_rows(rows)

    if window is None:
        window = 400
    window = int(window)
    if window > 0 and len(rows) > window:
        rows = rows[-window:]
    if len(rows) < MIN_ROWS:
        raise InsufficientDataError(
            "有效行情仅 %d 行（--window=%d），至少需要 %d 行"
            % (len(rows), window, MIN_ROWS)
        )

    close = _column(rows, "close")
    high = _column(rows, "high")
    low = _column(rows, "low")
    volume = _column(rows, "volume")

    if close.replace([np.inf, -np.inf], np.nan).dropna().empty:
        raise InsufficientDataError("quotes.rows 中没有有效的 close 数据")

    ma5 = ma(close, 5)
    ma10 = ma(close, 10)
    ma20 = ma(close, 20)
    ma60 = ma(close, 60)

    ema12 = ema(close, 12)
    ema26 = ema(close, 26)

    rsi14 = rsi(close, 14)

    macd_df = macd(close, fast=12, slow=26, signal=9)
    dif = macd_df["dif"]
    dea = macd_df["dea"]
    hist = macd_df["macd_hist"]

    boll_df = boll(close, n=20, k=2.0)

    atr14 = atr(high, low, close, n=14)

    kdj_df = kdj(high, low, close, n=9, m1=3, m2=3)

    vol_ratio = volume_ratio(volume, short=5, long=20)

    # ---- last：每个指标取最后一个有效值 ----
    last = {
        "close": _last_valid(close),
        "ma5": _last_valid(ma5),
        "ma10": _last_valid(ma10),
        "ma20": _last_valid(ma20),
        "ma60": _last_valid(ma60),
        "ema12": _last_valid(ema12),
        # 契约要求：不足 60 个交易日时 ema26 展示为 null。
        # MACD 内部仍按 12/26 周期计算（>=26 行即有值）。
        "ema26": _last_valid(ema26) if len(rows) >= 60 else None,
        "rsi14": _last_valid(rsi14),
        "macd": _last_valid(dif),
        "macd_signal": _last_valid(dea),
        "macd_hist": _last_valid(hist),
        "boll_upper": _last_valid(boll_df["boll_upper"]),
        "boll_mid": _last_valid(boll_df["boll_mid"]),
        "boll_lower": _last_valid(boll_df["boll_lower"]),
        "atr14": _last_valid(atr14),
        "kdj_k": _last_valid(kdj_df["kdj_k"]),
        "kdj_d": _last_valid(kdj_df["kdj_d"]),
        "kdj_j": _last_valid(kdj_df["kdj_j"]),
        "volume_ratio": _finite_or_none(vol_ratio),
    }

    # ---- series：最近 60 个交易日，长度对齐 ----
    tail = rows[-SERIES_LENGTH:]
    tail_start = len(rows) - len(tail)

    def _series_values(series: pd.Series) -> List[Optional[float]]:
        return [_finite_or_none(v) for v in series.iloc[tail_start:].tolist()]

    series = {
        "dates": [_date_string(r.get("date")) for r in tail],
        "close": _series_values(close),
        "ma20": _series_values(ma20),
        "rsi14": _series_values(rsi14),
        "macd_hist": _series_values(hist),
    }

    # ---- signals ----
    ma20_valid = _last_valid_values(ma20, 6)
    ma20_slope = (ma20_valid[-1] - ma20_valid[0]) if len(ma20_valid) >= 6 else None

    hist_valid = _last_valid_values(hist, 2)

    signals = {
        "trend": _classify_trend(last["close"], last["ma20"], last["ma60"], ma20_slope),
        "ma_alignment": _classify_ma_alignment(
            last["ma5"], last["ma10"], last["ma20"], last["ma60"]
        ),
        "rsi_state": _classify_rsi(last["rsi14"]),
        "macd_state": _classify_macd(hist_valid),
        "volume_state": _classify_volume(last["volume_ratio"]),
    }

    as_of = _date_string(rows[-1].get("date")) if rows else None
    if as_of == "":
        as_of = None

    return {
        "as_of": as_of,
        "last": last,
        "series": series,
        "signals": signals,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def default_output_path(input_path: str) -> str:
    """``bundle.json`` -> ``bundle.tech.json``。"""
    root, ext = os.path.splitext(input_path)
    if ext.lower() == ".json":
        return root + ".tech.json"
    return input_path + ".tech.json"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="读取 bundle JSON，计算 A 股技术指标并输出 *.tech.json（纯离线）"
    )
    parser.add_argument("--input", required=True, help="输入的 bundle JSON 路径")
    parser.add_argument("--out", default=None, help="输出路径，默认 {input}.tech.json")
    parser.add_argument("--window", type=int, default=400, help="最多使用的最近行情行数，默认 400")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    input_path = args.input
    output_path = args.out or default_output_path(input_path)

    try:
        with open(input_path, "r", encoding="utf-8") as handle:
            bundle = json.load(handle)
    except (OSError, ValueError) as exc:
        print("ERROR: 无法读取输入 JSON: %s (%s)" % (input_path, exc), file=sys.stderr)
        return 3

    if not isinstance(bundle, dict):
        print("ERROR: bundle 顶层必须是 JSON 对象: %s" % input_path, file=sys.stderr)
        return 3

    try:
        technical = compute_technical(bundle, window=args.window)
    except InsufficientDataError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 3
    except (TypeError, ValueError, KeyError) as exc:
        print("ERROR: 计算技术指标失败: %s" % exc, file=sys.stderr)
        return 3

    bundle["technical"] = technical
    safe_bundle = sanitize_json(bundle)

    try:
        parent = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(parent, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(safe_bundle, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
    except (OSError, ValueError) as exc:
        print("ERROR: 无法写入输出 JSON: %s (%s)" % (output_path, exc), file=sys.stderr)
        return 3

    print("WROTE %s" % output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
