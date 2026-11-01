#!/usr/bin/env python3
"""Fetch A-share research data and normalize it into the CONTRACT.md bundle.

Design goals
------------
* Python 3.9 compatible, standard library + optional akshare/pandas at runtime.
* Every remote module is isolated: one failing module never aborts the bundle.
* Every akshare call has a wall-clock timeout and tries several candidate
  interface names, because akshare renames/removes endpoints frequently.
* Missing values stay ``None`` (JSON ``null``); they are never silently 0.
* ``--offline`` never touches the network.

CLI (see CONTRACT.md):
    python scripts/fetch_data.py --symbol 600519 [--as-of YYYY-MM-DD] [--days 400]
        [--start YYYY-MM-DD] [--out PATH] [--source akshare|tushare]
        [--offline FIXTURE] [--strict]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import math
import os
import re
import sys
import time
import warnings as _pywarnings
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# urllib3 on macOS LibreSSL emits noisy NotOpenSSLWarning before requests run.
_pywarnings.filterwarnings("ignore")
# Some akshare endpoints use tqdm unconditionally; keep CLI output parseable.
os.environ.setdefault("TQDM_DISABLE", "1")

SCHEMA_VERSION = "1.0"
DEFAULT_DAYS = 400
FIN_PERIODS = 8
DEFAULT_NEWS_LIMIT = 20
try:
    AKSHARE_TIMEOUT = float(os.environ.get("ASHARE_FETCH_TIMEOUT", "20"))
except ValueError:
    AKSHARE_TIMEOUT = 20.0
try:
    AKSHARE_MAX_ATTEMPTS = int(os.environ.get("ASHARE_FETCH_RETRIES", "3"))
except ValueError:
    AKSHARE_MAX_ATTEMPTS = 3
_RETRY_DELAYS = (0.5, 1.5)
_CN_TZ = timezone(timedelta(hours=8))
_NULL_TEXT = {"", "-", "--", "none", "null", "nan", "nat", "n/a", "na", "无"}

_INDEX_DEFS = [
    ("上证指数", "sh000001"),
    ("深证成指", "sz399001"),
    ("创业板指", "sz399006"),
    ("科创50", "sh000688"),
]

_NAME_ALIASES = [
    "股票简称", "名称", "公司名称", "公司简称", "证券简称",
    "org_short_name_cn", "short_name", "name",
]
_INDUSTRY_ALIASES = [
    "行业", "所属行业", "行业名称", "所属行业名称", "industry",
]
_LIST_DATE_ALIASES = [
    "上市时间", "上市日期", "list_date", "issue_date", "listing_date",
]
_MARKET_CAP_ALIASES = [
    "总市值", "总市值(元)", "total_mv", "market_cap", "total_market_value",
]
_FLOAT_CAP_ALIASES = [
    "流通市值", "流通市值(元)", "circ_mv", "float_market_cap",
]
_TOTAL_SHARES_ALIASES = [
    "总股本", "总股本(股)", "总股本（股）", "total_shares", "total_share",
]
_FLOAT_SHARES_ALIASES = [
    "流通股", "流通股本", "流通股(股)", "流通股本(股)", "float_shares", "circulating_shares",
]
_CODE_ALIASES = ["代码", "股票代码", "证券代码", "symbol", "code", "ts_code"]
_DATE_ALIASES = ["日期", "date", "trade_date", "交易日", "时间"]
_PERIOD_ALIASES = [
    "REPORT_DATE", "报告期", "报告日期", "报告期末", "end_date", "日期", "date",
]
_ANN_DATE_ALIASES = [
    "NOTICE_DATE", "公告日期", "披露日期", "ann_date", "f_ann_date",
]


class FatalDataError(RuntimeError):
    """Raised when the bundle cannot be produced at all (no quotes)."""


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _china_now() -> datetime:
    return datetime.now(_CN_TZ)


def _now_iso() -> str:
    return _china_now().replace(microsecond=0).isoformat()


def _today_iso() -> str:
    return _china_now().date().isoformat()


def _parse_date(value: Any) -> Optional[date]:
    """Best-effort conversion of akshare/JSON date values to ``date``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        number = int(value)
        if number <= 0:
            return None
        # Epoch seconds / milliseconds.
        if number >= 10**12:
            try:
                return datetime.fromtimestamp(number / 1000.0, tz=timezone.utc).date()
            except (OverflowError, OSError, ValueError):
                return None
        if number >= 10**9:
            try:
                return datetime.fromtimestamp(number, tz=timezone.utc).date()
            except (OverflowError, OSError, ValueError):
                return None
        text = str(number)
    else:
        text = str(value).strip()
        if not text or text.lower() in _NULL_TEXT:
            return None
        # ISO-like text.
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            pass

    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        digits = digits[:8]
        try:
            return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
        except ValueError:
            return None
    return None


def _norm_date(value: Any) -> Optional[str]:
    parsed = _parse_date(value)
    return parsed.isoformat() if parsed else None


def _safe_float(value: Any) -> Optional[float]:
    """Convert loosely typed akshare values to float; null-like -> None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        return number

    text = str(value).strip().replace(",", "").replace("，", "")
    if not text or text.lower() in _NULL_TEXT:
        return None
    multiplier = 1.0
    if text.endswith("万亿"):
        multiplier = 1e12
        text = text[:-2]
    elif text.endswith("亿元") or text.endswith("亿"):
        multiplier = 1e8
        text = text[:-2] if text.endswith("亿元") else text[:-1]
    elif text.endswith("万元") or text.endswith("万"):
        multiplier = 1e4
        text = text[:-2] if text.endswith("万元") else text[:-1]
    text = text.replace("%", "").strip()
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number * multiplier


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        return int(round(number))
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in _NULL_TEXT:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return int(round(float(match.group(0))))
    except ValueError:
        return None


def _safe_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() in _NULL_TEXT:
        return None
    return text


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是"}


def _ratio_value(value: Any, threshold: float = 1.0) -> Optional[float]:
    """Convert a percentage-like source value into a decimal.

    Chinese data providers are inconsistent: some return 12.5 for 12.5%, some
    return 0.125.  Values above ``threshold`` are treated as percent.
    """
    number = _safe_float(value)
    if number is None:
        return None
    if abs(number) > threshold:
        return number / 100.0
    return number


def _percent_value(value: Any) -> Optional[float]:
    """Convert an akshare percentage column (always percent) to decimal."""
    number = _safe_float(value)
    if number is None:
        return None
    return number / 100.0


def _round_num(value: Any, ndigits: int) -> Optional[float]:
    number = _safe_float(value)
    if number is None:
        return None
    return round(number, ndigits)


def _round_half_up(value: Any, ndigits: int) -> Optional[float]:
    """Exchange-style 四舍五入 (avoids Python's banker's rounding)."""
    number = _safe_float(value)
    if number is None:
        return None
    quantum = Decimal("1").scaleb(-ndigits)
    rounded = Decimal(str(number)).quantize(quantum, rounding=ROUND_HALF_UP)
    return float(rounded)


def _short_exc(exc: BaseException) -> str:
    if isinstance(exc, (concurrent.futures.TimeoutError, TimeoutError)):
        return "超时"
    text = str(exc).strip().replace("\n", " ")
    if not text:
        text = exc.__class__.__name__
    return text[:180]


def _append_warning(warnings: List[str], message: str) -> None:
    if message not in warnings:
        warnings.append(message)


def _has_warning(warnings: Sequence[str], module: str) -> bool:
    prefix = module + ":"
    return any(str(item).startswith(prefix) for item in warnings)


def _record_get(record: Any, aliases: Sequence[str]) -> Any:
    if not isinstance(record, dict):
        return None
    for alias in aliases:
        if alias in record:
            return record[alias]
    lowered = {str(key).strip().lower(): value for key, value in record.items()}
    for alias in aliases:
        key = str(alias).strip().lower()
        if key in lowered:
            return lowered[key]
    return None


def _records(value: Any) -> List[Dict[str, Any]]:
    """Turn DataFrame/list/dict-like akshare results into list[dict]."""
    if value is None:
        return []
    if isinstance(value, dict):
        data = value.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        return [value]
    if isinstance(value, (list, tuple)):
        return [row for row in value if isinstance(row, dict)]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        for orient in ("records", "list"):
            try:
                converted = to_dict(orient)
            except (TypeError, ValueError):
                continue
            if isinstance(converted, list):
                return [row for row in converted if isinstance(row, dict)]
    return []


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    empty = getattr(value, "empty", None)
    if isinstance(empty, bool) and empty:
        return True
    try:
        return len(value) == 0
    except (TypeError, AttributeError):
        return False


def _sanitize(value: Any) -> Any:
    """Make a structure strictly JSON-safe (NaN/Inf -> null)."""
    if isinstance(value, dict):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _sanitize(item())
        except (TypeError, ValueError):
            pass
    return str(value)


# ---------------------------------------------------------------------------
# Symbol / board normalization
# ---------------------------------------------------------------------------


def resolve_symbol(symbol: Any) -> Dict[str, str]:
    """Normalize a ticker to 6-digit code, exchange and board.

    Accepts ``600519``, ``600519.SH``, ``SH600519``, ``sh600519`` and similar.
    Returns a dict with both plain and akshare-friendly spellings.
    """
    raw = str(symbol or "").strip()
    if not raw:
        raise ValueError("股票代码为空")

    digits_match = re.search(r"\d{6}", raw)
    if digits_match:
        code = digits_match.group(0)
    else:
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 6:
            code = digits
        elif 0 < len(digits) < 6:
            code = digits.zfill(6)
        else:
            raise ValueError("无法识别的股票代码: %s" % raw)

    suffix = None
    upper = raw.upper()
    if "." in upper:
        candidate = upper.split(".")[-1]
        if candidate in {"SH", "SSE", "SS"}:
            suffix = "SH"
        elif candidate in {"SZ", "SZSE"}:
            suffix = "SZ"
        elif candidate in {"BJ", "BSE"}:
            suffix = "BJ"
    elif upper[:2] in {"SH", "SZ", "BJ"}:
        suffix = upper[:2]

    by_prefix = None
    if code.startswith(("60", "68", "69", "90", "11", "50", "51", "52", "56", "58")):
        by_prefix = "SH"
    elif code.startswith(("00", "30", "20", "15", "16", "12", "18", "39")):
        by_prefix = "SZ"
    elif code.startswith(("8", "4", "92")):
        by_prefix = "BJ"

    exchange = suffix or by_prefix
    if exchange is None:
        raise ValueError("无法识别 %s 的交易所" % code)

    if code.startswith("68"):
        board = "star"
    elif code.startswith("30"):
        board = "chinext"
    elif exchange == "BJ" or code.startswith(("8", "4", "92")):
        board = "bse"
    else:
        board = "main"

    market = exchange.lower()
    return {
        "code": code,
        "symbol": code,
        "exchange": exchange,
        "suffix": exchange,
        "market": market,
        "akshare_market": market,
        "akshare_prefix": market + code,
        "akshare_symbol": market + code,
        "full_symbol": "%s.%s" % (code, exchange),
        "ts_code": "%s.%s" % (code, exchange),
        "board": board,
    }


def _info(symbol: Any) -> Dict[str, str]:
    if isinstance(symbol, dict) and symbol.get("code") and symbol.get("exchange"):
        return symbol
    if isinstance(symbol, dict):
        return resolve_symbol(symbol.get("symbol") or symbol.get("code"))
    return resolve_symbol(symbol)


# ---------------------------------------------------------------------------
# akshare invocation with timeout and candidate fallbacks
# ---------------------------------------------------------------------------


def _load_akshare() -> Any:
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on environment
        raise RuntimeError("akshare 未安装或导入失败: %s" % _short_exc(exc))
    return ak


def _call_with_timeout(func: Callable[..., Any], timeout: float,
                       kwargs: Dict[str, Any]) -> Any:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(func, **kwargs)
    try:
        return future.result(timeout=timeout)
    finally:
        # Do not block the whole fetch on a dead HTTP socket.
        executor.shutdown(wait=False)


def _is_retryable(exc: BaseException) -> bool:
    text = ("%s %s" % (exc.__class__.__name__, exc)).lower()
    tokens = (
        "connection", "remotedisconnected", "remote end closed", "timeout",
        "timed out", "ssl", "proxy", "temporarily", "reset by peer",
        "broken pipe", "max retries",
    )
    return any(token in text for token in tokens)


def _quiet_call(func: Callable[..., Any], kwargs: Dict[str, Any]) -> Any:
    """Run an akshare endpoint while suppressing tqdm/progress output."""
    with open(os.devnull, "w") as devnull:
        with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
            return func(**kwargs)


def _call_akshare(module: str,
                  candidates: Sequence[Tuple[Any, ...]],
                  warnings: List[str],
                  timeout: Optional[float] = None,
                  ak_module: Optional[Any] = None,
                  warn: bool = True,
                  retries: Optional[int] = None,
                  return_name: bool = False) -> Any:
    """Call the first non-empty akshare interface from ``candidates``.

    Candidate tuples are ``(name, kwargs)`` or ``(name, kwargs, options)``
    where ``options={"quiet": True}`` suppresses progress bars.  Each attempt
    runs in a worker thread with a wall-clock timeout.  Transient network
    failures are retried with 0.5s/1.5s backoff before moving to the next
    candidate interface.
    """
    try:
        ak = ak_module if ak_module is not None else _load_akshare()
    except Exception as exc:
        if warn:
            _append_warning(warnings, "%s: %s" % (module, _short_exc(exc)))
        return None
    max_attempts = max(int(retries if retries is not None else AKSHARE_MAX_ATTEMPTS), 1)
    errors: List[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        name = candidate[0]
        kwargs = candidate[1] if len(candidate) > 1 else {}
        options = candidate[2] if len(candidate) > 2 and isinstance(candidate[2], dict) else {}
        func = getattr(ak, name, None)
        if func is None:
            errors.append("%s: 接口不存在" % name)
            continue

        call_func = func
        if options.get("quiet"):
            call_func = lambda _func=func, _kwargs=kwargs: _quiet_call(_func, _kwargs)
            submit_kwargs: Dict[str, Any] = {}
        else:
            submit_kwargs = kwargs

        last_exc: Optional[BaseException] = None
        for attempt in range(max_attempts):
            try:
                result = _call_with_timeout(
                    call_func, timeout or AKSHARE_TIMEOUT, submit_kwargs)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if attempt + 1 < max_attempts and _is_retryable(exc):
                    time.sleep(_RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)])
                    continue
                break
        if last_exc is not None:
            errors.append("%s: %s" % (name, _short_exc(last_exc)))
            continue
        if _is_empty(result):
            errors.append("%s: 返回空" % name)
            continue
        if return_name:
            return result, name
        return result

    if warn:
        detail = "; ".join(errors) if errors else "无可用接口"
        _append_warning(warnings, "%s: 候选接口均失败（%s）" % (module, detail))
    return None


def safe_call(name: str, func: Callable[[], Any], warnings: List[str],
              default: Any = None) -> Any:
    """Run one data module, converting any failure into a warning."""
    try:
        result = func()
    except Exception as exc:
        _append_warning(warnings, "%s: %s" % (name, _short_exc(exc)))
        return default
    if result is None:
        if not _has_warning(warnings, name):
            _append_warning(warnings, "%s: 返回空结果" % name)
        return default
    return result


# ---------------------------------------------------------------------------
# Item/value maps (stock_individual_info_em, xueqiu, cninfo, ...)
# ---------------------------------------------------------------------------


def _item_value_map(raw: Any) -> Dict[str, Any]:
    mapping: Dict[str, Any] = {}
    for record in _records(raw):
        item = _safe_str(_record_get(record, ["item", "项目", "指标", "字段", "key", "选项"]))
        value = _record_get(record, ["value", "值", "数值", "val"])
        if item and value is not None:
            mapping[item] = value
        else:
            # Profile endpoints return one wide row.
            for key, val in record.items():
                if val is not None:
                    mapping.setdefault(str(key).strip(), val)
    return mapping


def _map_get(mapping: Dict[str, Any], aliases: Sequence[str]) -> Any:
    if not mapping:
        return None
    lowered = {str(key).strip().lower(): value for key, value in mapping.items()}
    for alias in aliases:
        if alias in mapping:
            value = mapping[alias]
            if value is not None and value != "":
                return value
        value = lowered.get(str(alias).strip().lower())
        if value is not None and value != "":
            return value
    return None


def _find_by_code(raw: Any, code: str) -> Optional[Dict[str, Any]]:
    for record in _records(raw):
        value = _safe_str(_record_get(record, _CODE_ALIASES))
        if not value:
            continue
        digits = re.sub(r"\D", "", value)
        if digits == code or digits.endswith(code):
            return record
    return None


# ---------------------------------------------------------------------------
# Tencent quote fallback (no extra dependency beyond requests)
# ---------------------------------------------------------------------------


def _http_get_text(url: str, warnings: List[str], module: str,
                   timeout: float = 20.0, retries: int = 3,
                   encoding: Optional[str] = None) -> Optional[str]:
    """HTTP GET with timeout and retry/backoff; failure becomes a warning."""
    try:
        import requests  # type: ignore
    except Exception as exc:
        _append_warning(warnings, "%s: requests 不可用: %s" % (module, _short_exc(exc)))
        return None

    attempts = max(int(retries), 1)
    last_exc: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            response = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 (A-Share Research Skill)"},
            )
            response.raise_for_status()
            if encoding:
                return response.content.decode(encoding, errors="replace")
            return response.text
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(_RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)])
                continue
            break
    if last_exc is not None:
        _append_warning(warnings, "%s: %s" % (module, _short_exc(last_exc)))
    return None


def _tencent_share_counts(fields: Sequence[str]) -> Tuple[Optional[float], Optional[float]]:
    """Extract total/float shares from the two large integers near the tail."""
    candidates: List[Tuple[int, int]] = []
    for index, item in enumerate(fields):
        text = str(item).strip()
        if re.fullmatch(r"\d{9,10}", text):
            value = int(text)
            if value > 100_000_000:
                candidates.append((index, value))
    if not candidates:
        return None, None

    counts: Dict[int, int] = {}
    for _index, value in candidates:
        counts[value] = counts.get(value, 0) + 1
    repeated = [value for value, count in counts.items() if count >= 2]
    if repeated:
        value = repeated[-1]
        return float(value), float(value)

    if len(candidates) >= 2:
        total = float(candidates[-2][1])
        floating = float(candidates[-1][1])
        return total, floating
    return float(candidates[-1][1]), None


def parse_tencent_snapshot(raw: Any) -> Dict[str, Any]:
    """Parse ``qt.gtimg.cn`` quote text into normalized fields.

    Pure parser (no network), used by both the live fetcher and offline tests.
    """
    if isinstance(raw, bytes):
        text = raw.decode("gbk", errors="replace")
    else:
        text = str(raw or "")
    match = re.search(r'v_[a-z]{2}\d{6}="(.*?)"(?:;|$)', text, re.S)
    if match:
        payload = match.group(1)
    else:
        payload = text.split('="', 1)[-1].rsplit('"', 1)[0]
    fields = payload.split("~")
    if len(fields) < 20:
        raise ValueError("腾讯行情字段不足")

    timestamp_index = None
    for index, value in enumerate(fields):
        if re.fullmatch(r"\d{14}", str(value).strip()):
            timestamp_index = index
            break
    if timestamp_index is None:
        raise ValueError("腾讯行情缺少 14 位时间戳字段")

    def at(offset: int) -> Optional[str]:
        index = timestamp_index + offset
        if 0 <= index < len(fields):
            return fields[index]
        return None

    total_shares, float_shares = _tencent_share_counts(fields)
    float_mv_yi = _safe_float(at(14))
    total_mv_yi = _safe_float(at(15))
    timestamp = (at(0) or "").strip()
    last_price = _safe_float(fields[3]) if len(fields) > 3 else None
    high = _safe_float(at(3))
    low = _safe_float(at(4))

    def money_yi_to_yuan(value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        return value * 1e8

    amount_wan = _safe_float(at(7))
    return {
        "source": "tencent",
        "name": _safe_str(fields[1]) if len(fields) > 1 else None,
        "code": _safe_str(fields[2]) if len(fields) > 2 else None,
        "trade_date": _norm_date(timestamp[:8]) if len(timestamp) >= 8 else None,
        "timestamp": timestamp or None,
        "price": _round_num(last_price, 2),
        "prev_close": _round_num(_safe_float(fields[4]), 2) if len(fields) > 4 else None,
        "open": _round_num(_safe_float(fields[5]), 2) if len(fields) > 5 else None,
        "high": _round_num(high if high is not None else last_price, 2),
        "low": _round_num(low if low is not None else last_price, 2),
        "change": _round_num(_safe_float(at(1)), 2),
        "pct_chg": _round_num(_percent_value(at(2)), 6),
        "volume": _round_num(_safe_float(at(6)), 0),  # 手
        "amount": _round_num(amount_wan * 1e4 if amount_wan is not None else None, 2),
        "turnover": _round_num(_percent_value(at(8)), 6),
        "pe_ttm": _round_num(_safe_float(at(9)), 6),
        "amplitude": _round_num(_percent_value(at(13)), 6),
        "float_market_cap": _round_num(money_yi_to_yuan(float_mv_yi), 2),
        "market_cap": _round_num(money_yi_to_yuan(total_mv_yi), 2),
        "pb": _round_num(_safe_float(at(16)), 6),
        "limit_up_price": _round_num(_safe_float(at(17)), 2),
        "limit_down_price": _round_num(_safe_float(at(18)), 2),
        "volume_ratio": _round_num(_safe_float(at(19)), 6),
        "total_shares": _round_num(total_shares, 0),
        "float_shares": _round_num(float_shares, 0),
    }


def fetch_tencent_snapshot(symbol: Any, warnings: Optional[List[str]] = None,
                           timeout: float = 20.0,
                           retries: int = 3) -> Optional[Dict[str, Any]]:
    """Fetch a Tencent real-time snapshot; used only as an Eastmoney fallback."""
    warnings = warnings if warnings is not None else []
    info = _info(symbol)
    url = "https://qt.gtimg.cn/q=%s" % info["akshare_symbol"]
    text = _http_get_text(
        url, warnings, "tencent", timeout=timeout, retries=retries, encoding="gbk",
    )
    if text is None:
        return None
    try:
        snapshot = parse_tencent_snapshot(text)
    except Exception as exc:
        _append_warning(warnings, "tencent: 解析失败: %s" % _short_exc(exc))
        return None
    snapshot["symbol"] = info["code"]
    return snapshot


def _tencent_lookup(symbol: Any, warnings: List[str],
                    fetcher: Optional[Callable[[], Any]] = None) -> Optional[Dict[str, Any]]:
    """safe_call wrapper around the Tencent snapshot (lazy/DI-friendly)."""
    if fetcher is not None:
        result = safe_call("tencent", fetcher, warnings, None)
    else:
        result = safe_call(
            "tencent",
            lambda: fetch_tencent_snapshot(symbol, warnings=warnings),
            warnings,
            None,
        )
    return result if isinstance(result, dict) else None


def fetch_tencent_kline(symbol: Any, start_date: date, as_of_date: date,
                        warnings: Optional[List[str]] = None,
                        timeout: float = 20.0,
                        retries: int = 3) -> Optional[List[Dict[str, Any]]]:
    """Tencent qfq daily kline fallback.  Kline has no amount/turnover fields."""
    warnings = warnings if warnings is not None else []
    info = _info(symbol)
    symbol_token = info["akshare_symbol"]
    span = max((as_of_date - start_date).days, 1)
    count = max(320, int(span * 0.8) + 30)
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
        "param=%s,day,,,%d,qfq" % (symbol_token, count)
    )
    text = _http_get_text(url, warnings, "quotes", timeout=timeout, retries=retries)
    if text is None:
        return None
    try:
        payload = json.loads(text)
    except Exception as exc:
        _append_warning(warnings, "quotes: 腾讯 kline 解析失败: %s" % _short_exc(exc))
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    node = data.get(symbol_token) if isinstance(data, dict) else None
    if not isinstance(node, dict):
        return None
    raw_rows = node.get("qfqday") or node.get("day") or []
    if not isinstance(raw_rows, list) or not raw_rows:
        _append_warning(warnings, "quotes: 腾讯 kline 返回空数据")
        return None

    records: List[Dict[str, Any]] = []
    for item in raw_rows:
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            continue
        records.append({
            "日期": item[0],
            "开盘": item[1],
            "收盘": item[2],
            "最高": item[3],
            "最低": item[4],
            "成交量": item[5],
        })
    if not records:
        _append_warning(warnings, "quotes: 腾讯 kline 无有效记录")
        return None
    _append_warning(
        warnings,
        "quotes: 使用腾讯 kline 备用行情（成交额/换手率缺失，已置 null）",
    )
    return records


# ---------------------------------------------------------------------------
# Basic information
# ---------------------------------------------------------------------------


def empty_basic() -> Dict[str, Any]:
    return {
        "name": None,
        "industry": None,
        "list_date": None,
        "is_st": False,
        "market_cap": None,
        "float_market_cap": None,
        "total_shares": None,
        "float_shares": None,
    }


def fetch_basic(symbol: Any, warnings: Optional[List[str]] = None,
                ak_module: Optional[Any] = None,
                tencent_fetcher: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
    warnings = warnings if warnings is not None else []
    info = _info(symbol)
    code = info["code"]
    prefix = info["akshare_prefix"]

    raw = _call_akshare(
        "basic",
        [
            ("stock_individual_info_em", {"symbol": code}),
            ("stock_individual_basic_info_xq", {"symbol": prefix.upper()}),
            ("stock_profile_cninfo", {"symbol": code}),
        ],
        warnings,
        ak_module=ak_module,
        warn=False,
    )
    items = _item_value_map(raw)

    name = _safe_str(_map_get(items, _NAME_ALIASES))
    industry = _safe_str(_map_get(items, _INDUSTRY_ALIASES))
    list_date = _norm_date(_map_get(items, _LIST_DATE_ALIASES))
    market_cap = _safe_float(_map_get(items, _MARKET_CAP_ALIASES))
    float_market_cap = _safe_float(_map_get(items, _FLOAT_CAP_ALIASES))
    total_shares = _safe_float(_map_get(items, _TOTAL_SHARES_ALIASES))
    float_shares = _safe_float(_map_get(items, _FLOAT_SHARES_ALIASES))

    # Market snapshot fallback.  It is a heavy endpoint, so only use it when
    # the lighter individual endpoints left important fields empty.
    if name is None or market_cap is None or float_market_cap is None:
        spot = _call_akshare(
            "basic",
            [("stock_zh_a_spot_em", {}), ("stock_zh_a_spot", {})],
            warnings,
            ak_module=ak_module,
            warn=False,
        )
        record = _find_by_code(spot, code)
        if record:
            name = name or _safe_str(_record_get(record, _NAME_ALIASES))
            market_cap = market_cap if market_cap is not None else _safe_float(
                _record_get(record, _MARKET_CAP_ALIASES))
            float_market_cap = (
                float_market_cap
                if float_market_cap is not None
                else _safe_float(_record_get(record, _FLOAT_CAP_ALIASES))
            )

    # Tencent fallback: used only after the Eastmoney individual/spot endpoints
    # failed to provide the core market-cap/share fields.
    if (
        name is None
        or market_cap is None
        or float_market_cap is None
        or total_shares is None
        or float_shares is None
    ):
        snapshot = _tencent_lookup(info, warnings, tencent_fetcher)
        if snapshot:
            used_tencent = False
            snapshot_name = _safe_str(snapshot.get("name"))
            if name is None and snapshot_name:
                name = snapshot_name
                used_tencent = True
            if market_cap is None and snapshot.get("market_cap") is not None:
                market_cap = _safe_float(snapshot.get("market_cap"))
                used_tencent = True
            if float_market_cap is None and snapshot.get("float_market_cap") is not None:
                float_market_cap = _safe_float(snapshot.get("float_market_cap"))
                used_tencent = True
            if total_shares is None and snapshot.get("total_shares") is not None:
                total_shares = _safe_float(snapshot.get("total_shares"))
                used_tencent = True
            if float_shares is None and snapshot.get("float_shares") is not None:
                float_shares = _safe_float(snapshot.get("float_shares"))
                used_tencent = True
            if used_tencent:
                _append_warning(warnings, "basic: 使用腾讯行情快照补全基础字段")

    if name is None:
        code_names = _call_akshare(
            "basic",
            [("stock_info_a_code_name", {})],
            warnings,
            ak_module=ak_module,
            warn=False,
        )
        record = _find_by_code(code_names, code)
        if record:
            name = _safe_str(_record_get(record, _NAME_ALIASES))

    is_st = bool(re.search(r"(?i)(^|\*)ST|退", name or ""))
    basic: Dict[str, Any] = {
        "name": name,
        "industry": industry,
        "list_date": list_date,
        "is_st": is_st,
        "market_cap": _round_num(market_cap, 2),
        "float_market_cap": _round_num(float_market_cap, 2),
        "total_shares": _round_num(total_shares, 0),
        "float_shares": _round_num(float_shares, 0),
    }

    missing = []
    for key, label in (
        ("name", "股票名称"),
        ("industry", "行业"),
        ("list_date", "上市日期"),
        ("market_cap", "总市值"),
        ("float_market_cap", "流通市值"),
        ("total_shares", "总股本"),
        ("float_shares", "流通股"),
    ):
        if basic.get(key) is None:
            missing.append(label)
    if missing:
        _append_warning(warnings, "basic: 字段缺失（%s）" % "、".join(missing))
    return basic


# ---------------------------------------------------------------------------
# Quotes (qfq daily bars)
# ---------------------------------------------------------------------------


def empty_quotes() -> Dict[str, Any]:
    return {"adjust": "qfq", "period": "daily", "rows": []}


def fetch_quotes(symbol: Any, as_of: Optional[str] = None,
                 days: int = DEFAULT_DAYS, start: Optional[str] = None,
                 warnings: Optional[List[str]] = None,
                 ak_module: Optional[Any] = None,
                 tencent_kline_fetcher: Optional[Callable[[], Any]] = None
                 ) -> Optional[Dict[str, Any]]:
    warnings = warnings if warnings is not None else []
    info = _info(symbol)
    code = info["code"]
    prefix = info["akshare_prefix"]

    as_of_date = _parse_date(as_of) or _china_now().date()
    if start:
        start_date = _parse_date(start)
        if start_date is None:
            raise ValueError("无效的 --start 日期: %s" % start)
    else:
        start_date = as_of_date - timedelta(days=max(int(days), 1))
    if start_date > as_of_date:
        _append_warning(warnings, "quotes: start 晚于 as_of，已交换区间")
        start_date, as_of_date = as_of_date, start_date
    start_compact = start_date.strftime("%Y%m%d")
    end_compact = as_of_date.strftime("%Y%m%d")
    start_iso = start_date.isoformat()
    as_of_iso = as_of_date.isoformat()

    raw_result = _call_akshare(
        "quotes",
        [
            (
                "stock_zh_a_hist",
                {
                    "symbol": code,
                    "period": "daily",
                    "start_date": start_compact,
                    "end_date": end_compact,
                    "adjust": "qfq",
                },
            ),
            (
                "stock_zh_a_hist_tx",
                {
                    "symbol": prefix,
                    "start_date": start_compact,
                    "end_date": end_compact,
                    "adjust": "qfq",
                },
            ),
            (
                "stock_zh_a_daily",
                {
                    "symbol": prefix,
                    "start_date": start_compact,
                    "end_date": end_compact,
                    "adjust": "qfq",
                },
            ),
        ],
        warnings,
        ak_module=ak_module,
        return_name=True,
    )
    if isinstance(raw_result, tuple) and len(raw_result) == 2:
        raw, quote_source = raw_result
    else:
        raw, quote_source = raw_result, None
    if raw is None:
        # Tencent qfq kline fallback (no amount/turnover fields).
        if tencent_kline_fetcher is not None:
            raw = safe_call("quotes", tencent_kline_fetcher, warnings, None)
        else:
            raw = safe_call(
                "quotes",
                lambda: fetch_tencent_kline(
                    info, start_date, as_of_date, warnings=warnings,
                ),
                warnings,
                None,
            )
        quote_source = "tencent_kline"
        if raw is None:
            return None
    if quote_source and quote_source not in ("stock_zh_a_hist", "tencent_kline"):
        _append_warning(
            warnings,
            "quotes: stock_zh_a_hist 不可用，已回退 %s" % quote_source,
        )

    by_date: Dict[str, Dict[str, Any]] = {}
    missing_price_dates: List[str] = []
    for record in _records(raw):
        trade_date = _norm_date(_record_get(record, _DATE_ALIASES))
        if not trade_date:
            continue
        if trade_date < start_iso or trade_date > as_of_iso:
            continue

        open_px = _safe_float(_record_get(record, ["开盘", "open"]))
        high_px = _safe_float(_record_get(record, ["最高", "high"]))
        low_px = _safe_float(_record_get(record, ["最低", "low"]))
        close_px = _safe_float(_record_get(record, ["收盘", "close"]))
        # Canonical volume is shares.  akshare stock_zh_a_hist reports 手
        # (1 手 = 100 股); the TX/Sina/Tencent-kline fallbacks already report
        # shares.  Amount is 元 in all these sources.
        volume = _safe_float(_record_get(record, ["成交量", "volume", "vol"]))
        if volume is not None and quote_source == "stock_zh_a_hist":
            volume *= 100.0
        amount = _safe_float(_record_get(record, ["成交额", "amount"]))
        pct_raw = _record_get(record, ["涨跌幅", "pct_chg", "change_percent", "change_pct"])
        turnover_raw = _record_get(record, ["换手率", "turnover", "turnover_rate"])

        if close_px is None or close_px <= 0:
            # Suspended / empty bar.  Do not fabricate prices.
            if any(value is not None for value in (open_px, high_px, low_px)) or (
                volume is not None and volume > 0
            ):
                missing_price_dates.append(trade_date)
            continue

        # EM hist: 涨跌幅/换手率 are percent.  TX/Sina: turnover is already a
        # decimal; pct_chg is absent and gets computed from closes below.
        if pct_raw is not None:
            pct_chg = _percent_value(pct_raw) if quote_source == "stock_zh_a_hist" else _safe_float(pct_raw)
        else:
            pct_chg = None
        turnover = _safe_float(turnover_raw)
        if turnover is not None and quote_source == "stock_zh_a_hist":
            turnover = turnover / 100.0
        by_date[trade_date] = {
            "date": trade_date,
            "open": _round_num(open_px, 2),
            "high": _round_num(high_px, 2),
            "low": _round_num(low_px, 2),
            "close": _round_num(close_px, 2),
            "volume": _round_num(volume, 0),
            "amount": _round_num(amount, 2),
            "pct_chg": _round_num(pct_chg, 6),
            "turnover": _round_num(turnover, 6),
        }

    rows = [by_date[key] for key in sorted(by_date)]
    if not rows:
        _append_warning(warnings, "quotes: 未获取到 as_of 之前的行情数据")
        return None

    previous_close: Optional[float] = None
    for row in rows:
        if row["pct_chg"] is None and previous_close not in (None, 0):
            row["pct_chg"] = round((row["close"] - previous_close) / previous_close, 6)
        previous_close = row["close"]

    if missing_price_dates:
        _append_warning(
            warnings,
            "quotes: 剔除 %d 条无有效价格的停牌/空记录（%s）"
            % (len(missing_price_dates), "、".join(missing_price_dates[:5])),
        )
    zero_volume = [row["date"] for row in rows if row["volume"] is not None and row["volume"] == 0]
    if zero_volume:
        _append_warning(
            warnings,
            "quotes: %d 个交易日成交量为 0（疑似停牌/无成交），已保留（%s）"
            % (len(zero_volume), "、".join(zero_volume[:5])),
        )
    missing_volume = sum(1 for row in rows if row["volume"] is None)
    if missing_volume:
        _append_warning(warnings, "quotes: %d 个交易日成交量缺失" % missing_volume)
    missing_amount = sum(1 for row in rows if row["amount"] is None)
    if missing_amount:
        _append_warning(warnings, "quotes: %d 个交易日成交额缺失（已置 null）" % missing_amount)
    missing_turnover = sum(1 for row in rows if row["turnover"] is None)
    if missing_turnover:
        _append_warning(warnings, "quotes: %d 个交易日换手率缺失（已置 null）" % missing_turnover)
    if len(rows) < 30:
        _append_warning(warnings, "quotes: 仅 %d 个交易日，少于 30，技术指标可能不足" % len(rows))

    return {"adjust": "qfq", "period": "daily", "rows": rows}


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------


def empty_valuation() -> Dict[str, Any]:
    return {
        "pe_ttm": None,
        "pb": None,
        "ps_ttm": None,
        "dividend_yield": None,
        "total_mv": None,
        "circ_mv": None,
    }


def fetch_valuation(symbol: Any, basic: Optional[Dict[str, Any]] = None,
                    warnings: Optional[List[str]] = None,
                    ak_module: Optional[Any] = None,
                    as_of: Optional[str] = None,
                    tencent_fetcher: Optional[Callable[[], Any]] = None
                    ) -> Dict[str, Any]:
    warnings = warnings if warnings is not None else []
    info = _info(symbol)
    code = info["code"]
    prefix = info["akshare_prefix"]
    valuation = empty_valuation()
    used_dynamic_pe = False
    if as_of and _norm_date(as_of) != _today_iso():
        _append_warning(warnings, "valuation: 估值为最新快照，非 as_of 当日")

    # Confirmed working in akshare 1.18.88: batch market snapshot filtered by
    # code.  It carries 市盈率-动态 / 市净率 / 总市值 / 流通市值.
    raw = _call_akshare(
        "valuation",
        [("stock_zh_a_spot_em", {}), ("stock_zh_a_spot", {})],
        warnings,
        ak_module=ak_module,
        warn=False,
    )
    record = _find_by_code(raw, code)
    if record:
        dynamic_pe = _safe_float(_record_get(record, ["市盈率-动态", "市盈率", "pe"]))
        if dynamic_pe is not None:
            valuation["pe_ttm"] = dynamic_pe
            used_dynamic_pe = True
        valuation["pb"] = _safe_float(_record_get(record, ["市净率", "pb"]))
        valuation["total_mv"] = _safe_float(_record_get(record, ["总市值", "total_mv"]))
        valuation["circ_mv"] = _safe_float(_record_get(record, ["流通市值", "circ_mv"]))

    # Eastmoney spot failed (or lacks PE/PB): fall back to Tencent.
    if valuation["pe_ttm"] is None or valuation["pb"] is None:
        snapshot = _tencent_lookup(info, warnings, tencent_fetcher)
        if snapshot:
            used_tencent = False
            if valuation["pe_ttm"] is None and snapshot.get("pe_ttm") is not None:
                valuation["pe_ttm"] = _safe_float(snapshot.get("pe_ttm"))
                used_tencent = True
            if valuation["pb"] is None and snapshot.get("pb") is not None:
                valuation["pb"] = _safe_float(snapshot.get("pb"))
                used_tencent = True
            if valuation["total_mv"] is None and snapshot.get("market_cap") is not None:
                valuation["total_mv"] = _safe_float(snapshot.get("market_cap"))
                used_tencent = True
            if valuation["circ_mv"] is None and snapshot.get("float_market_cap") is not None:
                valuation["circ_mv"] = _safe_float(snapshot.get("float_market_cap"))
                used_tencent = True
            if used_tencent:
                _append_warning(warnings, "valuation: pe_ttm/pb 使用腾讯行情快照")

    # Secondary source for PS / dividend yield, which the spot snapshot lacks.
    if any(valuation[key] is None for key in ("pe_ttm", "pb", "ps_ttm", "dividend_yield")):
        raw = _call_akshare(
            "valuation",
            [("stock_individual_basic_info_xq", {"symbol": prefix.upper()})],
            warnings,
            ak_module=ak_module,
            warn=False,
        )
        items = _item_value_map(raw)
        valuation["pe_ttm"] = valuation["pe_ttm"] if valuation["pe_ttm"] is not None else _safe_float(
            _map_get(items, ["市盈率(TTM)", "市盈率(动)", "市盈率", "pe_ttm", "pe"]))
        valuation["pb"] = valuation["pb"] if valuation["pb"] is not None else _safe_float(
            _map_get(items, ["市净率", "pb"]))
        valuation["ps_ttm"] = valuation["ps_ttm"] if valuation["ps_ttm"] is not None else _safe_float(
            _map_get(items, ["市销率(TTM)", "市销率", "ps_ttm", "ps"]))
        valuation["dividend_yield"] = (
            valuation["dividend_yield"]
            if valuation["dividend_yield"] is not None
            else _ratio_value(_map_get(items, ["股息率(TTM)", "股息率", "股息率(%)"]), 0.5)
        )
        valuation["total_mv"] = valuation["total_mv"] if valuation["total_mv"] is not None else _safe_float(
            _map_get(items, ["总市值", "total_mv"]))
        valuation["circ_mv"] = valuation["circ_mv"] if valuation["circ_mv"] is not None else _safe_float(
            _map_get(items, ["流通市值", "circ_mv"]))

    if basic:
        if valuation["total_mv"] is None:
            valuation["total_mv"] = basic.get("market_cap")
        if valuation["circ_mv"] is None:
            valuation["circ_mv"] = basic.get("float_market_cap")

    if used_dynamic_pe:
        _append_warning(warnings, "valuation: pe_ttm 使用动态市盈率近似")
    missing = [key for key, value in valuation.items() if value is None]
    if missing:
        _append_warning(warnings, "valuation: 字段缺失（%s）" % "、".join(missing))
    for key in valuation:
        valuation[key] = _round_num(valuation[key], 6)
    return valuation


# ---------------------------------------------------------------------------
# Financial statements / fundamentals
# ---------------------------------------------------------------------------


def empty_financials() -> Dict[str, Any]:
    return {
        "fundamentals": {"periods": []},
        "financials": {"income": [], "balance": [], "cashflow": []},
    }


def _wide_report_records(raw: Any) -> List[Dict[str, Any]]:
    """Transpose Sina-style wide statements (one column per report period)."""
    records = _records(raw)
    if not records:
        return []
    has_period = any(_parse_date(_record_get(rec, _PERIOD_ALIASES)) for rec in records[:5])
    if has_period:
        return records

    sample = records[0]
    date_columns = []
    for key in sample.keys():
        text = str(key)
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 8 and _parse_date(text):
            date_columns.append(key)
    if not date_columns:
        return records

    transposed: List[Dict[str, Any]] = []
    for column in date_columns:
        row: Dict[str, Any] = {"报告期": _norm_date(column)}
        for record in records:
            label = _record_get(record, ["报告日", "项目", "指标", "科目", "选项"])
            if label is None:
                continue
            row[str(label).strip()] = _record_get(record, [column])
        transposed.append(row)
    return transposed


def _statement_map(raw: Any) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for record in _wide_report_records(raw):
        period = _norm_date(_record_get(record, _PERIOD_ALIASES))
        if period:
            result[period] = record
    return result


def _ann_date(record: Optional[Dict[str, Any]]) -> Optional[str]:
    if not record:
        return None
    return _norm_date(_record_get(record, _ANN_DATE_ALIASES))


def _report_type(period: str) -> str:
    return "annual" if period.endswith("12-31") else "quarter"


def _financial_value(record: Optional[Dict[str, Any]], aliases: Sequence[str]) -> Optional[float]:
    if not record:
        return None
    return _safe_float(_record_get(record, aliases))


def fetch_financials(symbol: Any, as_of: Optional[str] = None,
                     warnings: Optional[List[str]] = None,
                     ak_module: Optional[Any] = None,
                     limit: int = FIN_PERIODS) -> Dict[str, Any]:
    warnings = warnings if warnings is not None else []
    info = _info(symbol)
    code = info["code"]
    prefix = info["akshare_prefix"]
    as_of_iso = _norm_date(as_of) or _today_iso()
    as_of_year = (_parse_date(as_of_iso) or _china_now().date()).year

    income_raw = _call_akshare(
        "finance",
        [
            ("stock_profit_sheet_by_report_em", {"symbol": prefix.upper()}),
            ("stock_profit_sheet_by_report_em", {"symbol": code}),
            ("stock_profit_sheet_by_yearly_em", {"symbol": prefix.upper()}),
            ("stock_financial_report_sina", {"stock": prefix, "symbol": "利润表"}),
        ],
        warnings,
        ak_module=ak_module,
        warn=False,
    )
    balance_raw = _call_akshare(
        "finance",
        [
            ("stock_balance_sheet_by_report_em", {"symbol": prefix.upper()}),
            ("stock_balance_sheet_by_report_em", {"symbol": code}),
            ("stock_balance_sheet_by_yearly_em", {"symbol": prefix.upper()}),
            ("stock_financial_report_sina", {"stock": prefix, "symbol": "资产负债表"}),
        ],
        warnings,
        ak_module=ak_module,
        warn=False,
    )
    cashflow_raw = _call_akshare(
        "finance",
        [
            ("stock_cash_flow_sheet_by_report_em", {"symbol": prefix.upper()}),
            ("stock_cash_flow_sheet_by_report_em", {"symbol": code}),
            ("stock_cash_flow_sheet_by_yearly_em", {"symbol": prefix.upper()}),
            ("stock_financial_report_sina", {"stock": prefix, "symbol": "现金流量表"}),
        ],
        warnings,
        ak_module=ak_module,
        warn=False,
    )
    metrics_raw = _call_akshare(
        "finance",
        [
            # Confirmed in akshare 1.18.88: wide table with 选项/指标/各报告期.
            ("stock_financial_abstract", {"symbol": code}),
            ("stock_financial_abstract_ths", {"symbol": code, "indicator": "按报告期"}),
            (
                "stock_financial_analysis_indicator",
                {"symbol": code, "start_year": str(max(2000, as_of_year - 3))},
                {"quiet": True},  # this endpoint prints a tqdm bar
            ),
        ],
        warnings,
        ak_module=ak_module,
        warn=False,
    )

    income_map = _statement_map(income_raw)
    balance_map = _statement_map(balance_raw)
    cashflow_map = _statement_map(cashflow_raw)
    metrics_map = _statement_map(metrics_raw)

    statement_failed = []
    if not income_map:
        statement_failed.append("利润表")
    if not balance_map:
        statement_failed.append("资产负债表")
    if not cashflow_map:
        statement_failed.append("现金流量表")
    if statement_failed and metrics_map:
        _append_warning(
            warnings,
            "finance: %s 接口不可用，已使用财务摘要补全" % "、".join(statement_failed),
        )

    all_periods = set(income_map) | set(balance_map) | set(cashflow_map) | set(metrics_map)
    candidate_periods = sorted(
        (period for period in all_periods if period <= as_of_iso), reverse=True
    )

    selected: List[Tuple[str, Optional[str]]] = []
    unconfirmed = 0
    for period in candidate_periods:
        ann_dates = [
            value for value in (
                _ann_date(income_map.get(period)),
                _ann_date(balance_map.get(period)),
                _ann_date(cashflow_map.get(period)),
                _ann_date(metrics_map.get(period)),
            )
            if value
        ]
        ann_date = min(ann_dates) if ann_dates else None
        if ann_date and ann_date > as_of_iso:
            # Not yet disclosed at as_of; avoid look-ahead bias.
            continue
        if not ann_date:
            unconfirmed += 1
        selected.append((period, ann_date))
        if len(selected) >= max(int(limit), 1):
            break

    if not selected:
        _append_warning(warnings, "finance: 未获取到 <= as_of 的已披露报告期")
        return empty_financials()

    if unconfirmed:
        _append_warning(
            warnings,
            "finance: 无法确认 %d 个报告期的披露日期，已按报告期<=as_of 过滤" % unconfirmed,
        )

    fundamentals: List[Dict[str, Any]] = []
    income_rows: List[Dict[str, Any]] = []
    balance_rows: List[Dict[str, Any]] = []
    cashflow_rows: List[Dict[str, Any]] = []
    estimated_roe = 0

    for period, _ann in selected:
        income_rec = income_map.get(period)
        balance_rec = balance_map.get(period)
        cashflow_rec = cashflow_map.get(period)
        metrics_rec = metrics_map.get(period)

        revenue = _financial_value(income_rec, [
            "TOTAL_OPERATE_INCOME", "OPERATE_INCOME", "营业总收入", "营业收入",
            "主营业务收入(元)", "主营业务收入", "REVENUE", "revenue", "oper_rev",
            "total_revenue",
        ])
        net_profit = _financial_value(income_rec, [
            "PARENT_NETPROFIT", "NETPROFIT", "归属于母公司所有者的净利润", "归母净利润",
            "净利润(元)", "净利润", "n_income_attr_p", "net_profit", "NET_PROFIT",
        ])
        operating_profit = _financial_value(income_rec, [
            "OPERATE_PROFIT", "营业利润(元)", "营业利润", "operate_profit", "oper_profit",
        ])
        oper_cost = _financial_value(income_rec, [
            "OPERATE_COST", "TOTAL_OPERATE_COST", "营业成本", "oper_cost",
        ])
        total_assets = _financial_value(balance_rec, [
            "TOTAL_ASSETS", "资产总计", "总资产", "total_assets",
        ])
        total_liabilities = _financial_value(balance_rec, [
            "TOTAL_LIABILITIES", "负债合计", "总负债", "total_liab",
        ])
        equity = _financial_value(balance_rec, [
            "TOTAL_PARENT_EQUITY", "TOTAL_EQUITY", "归属于母公司股东权益合计",
            "所有者权益合计", "股东权益合计", "total_hldr_eqy_exc_min_int", "equity",
        ])
        ocf = _financial_value(cashflow_rec, [
            "NETCASH_OPERATE", "经营活动产生的现金流量净额", "经营活动现金流量净额",
            "经营现金流量净额", "ocf", "n_cashflow_act",
        ])
        icf = _financial_value(cashflow_rec, [
            "NETCASH_INVEST", "投资活动产生的现金流量净额", "投资现金流量净额",
            "icf", "n_cashflow_inv_act",
        ])
        fcf = _financial_value(cashflow_rec, [
            "FREE_CASH_FLOW", "自由现金流量", "fcf",
        ])

        # Missing statement fields fall back to the wide financial abstract
        # (stock_financial_abstract) / analysis indicator table.
        revenue = revenue if revenue is not None else _financial_value(metrics_rec, [
            "营业总收入", "营业收入", "主营业务收入(元)", "主营业务收入",
            "REVENUE", "revenue",
        ])
        net_profit = net_profit if net_profit is not None else _financial_value(metrics_rec, [
            "归母净利润", "归属于母公司股东的净利润", "净利润(元)", "净利润",
            "net_profit", "n_income_attr_p",
        ])
        operating_profit = operating_profit if operating_profit is not None else _financial_value(
            metrics_rec, ["营业利润(元)", "营业利润", "operate_profit"])
        oper_cost = oper_cost if oper_cost is not None else _financial_value(
            metrics_rec, ["营业成本", "oper_cost"])
        total_assets = total_assets if total_assets is not None else _financial_value(
            metrics_rec, ["总资产", "资产总计", "total_assets"])
        total_liabilities = total_liabilities if total_liabilities is not None else _financial_value(
            metrics_rec, ["总负债", "负债合计", "total_liab"])
        equity = equity if equity is not None else _financial_value(
            metrics_rec, ["股东权益合计", "所有者权益合计", "equity"])
        ocf = ocf if ocf is not None else _financial_value(metrics_rec, [
            "经营活动产生的现金流量净额", "经营现金流量净额", "ocf", "n_cashflow_act",
        ])
        icf = icf if icf is not None else _financial_value(metrics_rec, [
            "投资活动产生的现金流量净额", "投资现金流量净额", "icf", "n_cashflow_inv_act",
        ])

        roe = _ratio_value(_record_get(metrics_rec or {}, [
            "净资产收益率", "加权净资产收益率", "净资产收益率(%)", "ROE", "roe",
        ]))
        gross_margin = _ratio_value(_record_get(metrics_rec or {}, [
            "销售毛利率", "毛利率", "销售毛利率(%)", "gross_margin",
        ]))
        net_margin = _ratio_value(_record_get(metrics_rec or {}, [
            "销售净利率", "净利率", "销售净利率(%)", "net_margin",
        ]))
        debt_ratio = _ratio_value(_record_get(metrics_rec or {}, [
            "资产负债率", "资产负债率(%)", "debt_ratio",
        ]))

        if gross_margin is None and revenue not in (None, 0) and oper_cost is not None:
            gross_margin = (revenue - oper_cost) / revenue
        if net_margin is None and revenue not in (None, 0) and net_profit is not None:
            net_margin = net_profit / revenue
        if debt_ratio is None and total_assets not in (None, 0) and total_liabilities is not None:
            debt_ratio = total_liabilities / total_assets
        if roe is None and net_profit is not None and equity not in (None, 0):
            roe = net_profit / equity
            estimated_roe += 1

        fundamentals.append({
            "period": period,
            "report_type": _report_type(period),
            "roe": _round_num(roe, 6),
            "gross_margin": _round_num(gross_margin, 6),
            "net_margin": _round_num(net_margin, 6),
            "debt_ratio": _round_num(debt_ratio, 6),
            "revenue": _round_num(revenue, 2),
            "net_profit": _round_num(net_profit, 2),
            "ocf": _round_num(ocf, 2),
        })
        income_rows.append({
            "period": period,
            "revenue": _round_num(revenue, 2),
            "net_profit": _round_num(net_profit, 2),
            "operating_profit": _round_num(operating_profit, 2),
        })
        balance_rows.append({
            "period": period,
            "total_assets": _round_num(total_assets, 2),
            "total_liabilities": _round_num(total_liabilities, 2),
            "equity": _round_num(equity, 2),
        })
        cashflow_rows.append({
            "period": period,
            "ocf": _round_num(ocf, 2),
            "icf": _round_num(icf, 2),
            "fcf": _round_num(fcf, 2),
        })

    if estimated_roe:
        _append_warning(
            warnings,
            "finance: roe 由净利润/净资产估算（%d 个报告期）" % estimated_roe,
        )

    total_periods = len(fundamentals)
    missing_fields: Dict[str, int] = {}
    for field in ("roe", "gross_margin", "net_margin", "debt_ratio", "revenue", "net_profit", "ocf"):
        count = sum(1 for row in fundamentals if row[field] is None)
        if count:
            missing_fields[field] = count
    if missing_fields:
        detail = "、".join("%s %d/%d" % (key, value, total_periods)
                           for key, value in sorted(missing_fields.items()))
        _append_warning(warnings, "finance: 字段缺失（%s）" % detail)

    statement_checks = (
        ("利润表", income_rows, ("revenue", "net_profit", "operating_profit")),
        ("资产负债表", balance_rows, ("total_assets", "total_liabilities", "equity")),
        ("现金流量表", cashflow_rows, ("ocf", "icf", "fcf")),
    )
    for label, rows, fields in statement_checks:
        if not rows:
            _append_warning(warnings, "finance: %s 数据缺失" % label)
            continue
        if all(row.get(fields[0]) is None for row in rows):
            _append_warning(warnings, "finance: %s 数据缺失" % label)
            continue
        missing_detail = []
        for field in fields:
            count = sum(1 for row in rows if row.get(field) is None)
            if count:
                missing_detail.append("%s %d/%d" % (field, count, len(rows)))
        if missing_detail:
            _append_warning(
                warnings,
                "finance: %s 字段缺失（%s）" % (label, "、".join(missing_detail)),
            )

    return {
        "fundamentals": {"periods": fundamentals},
        "financials": {
            "income": income_rows,
            "balance": balance_rows,
            "cashflow": cashflow_rows,
        },
    }


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------


def fetch_news(symbol: Any, as_of: Optional[str] = None,
               warnings: Optional[List[str]] = None,
               ak_module: Optional[Any] = None,
               limit: int = DEFAULT_NEWS_LIMIT) -> List[Dict[str, Any]]:
    warnings = warnings if warnings is not None else []
    info = _info(symbol)
    code = info["code"]
    as_of_iso = _norm_date(as_of) or _today_iso()

    raw = _call_akshare(
        "news",
        [
            ("stock_news_em", {"symbol": code}),
            ("stock_news_main_cx", {}),
        ],
        warnings,
        ak_module=ak_module,
    )
    if raw is None:
        return []

    items: List[Dict[str, Any]] = []
    seen_titles = set()
    undated = 0
    for record in _records(raw):
        news_date = _norm_date(_record_get(record, [
            "发布时间", "时间", "日期", "date", "pub_time", "datetime", "publish_time",
        ]))
        if not news_date:
            undated += 1
            continue
        if news_date > as_of_iso:
            continue
        title = _safe_str(_record_get(record, ["新闻标题", "标题", "title"])) or ""
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        summary = _safe_str(_record_get(record, [
            "新闻内容", "内容", "摘要", "summary", "digest", "content",
        ])) or ""
        if len(summary) > 300:
            summary = summary[:297] + "..."
        items.append({
            "date": news_date,
            "title": title,
            "summary": summary,
            "source": _safe_str(_record_get(record, [
                "文章来源", "来源", "source", "media_name",
            ])) or "",
            "url": _safe_str(_record_get(record, [
                "新闻链接", "链接", "url", "link",
            ])) or "",
        })

    items.sort(key=lambda item: item["date"], reverse=True)
    items = items[:max(int(limit), 1)]
    if undated:
        _append_warning(warnings, "news: %d 条新闻缺少日期，已忽略" % undated)
    if not items:
        _append_warning(warnings, "news: 未获取到 as_of 之前的新闻")
    return items


# ---------------------------------------------------------------------------
# Sector
# ---------------------------------------------------------------------------


def empty_sector() -> Dict[str, Any]:
    return {
        "name": None,
        "index_pct_chg": None,
        "rank": None,
        "rank_total": None,
        "main_net_inflow": None,
    }


def fetch_sector(symbol: Any, basic: Optional[Dict[str, Any]] = None,
                 warnings: Optional[List[str]] = None,
                 ak_module: Optional[Any] = None) -> Dict[str, Any]:
    warnings = warnings if warnings is not None else []
    industry = _safe_str((basic or {}).get("industry"))
    sector = empty_sector()
    sector["name"] = industry

    board_raw = _call_akshare(
        "sector",
        [
            ("stock_board_industry_name_em", {}),
            ("stock_board_industry_name_ths", {}),
        ],
        warnings,
        ak_module=ak_module,
        warn=False,
    )
    board_records = _records(board_raw)
    matched: Optional[Dict[str, Any]] = None
    if industry and board_records:
        for record in board_records:
            name = _safe_str(_record_get(record, ["板块名称", "名称", "name"]))
            if name == industry:
                matched = record
                break
        if matched is None:
            for record in board_records:
                name = _safe_str(_record_get(record, ["板块名称", "名称", "name"]))
                if name and (industry in name or name in industry):
                    matched = record
                    break

    if matched:
        sector["index_pct_chg"] = _percent_value(
            _record_get(matched, ["涨跌幅", "涨跌幅(%)", "pct_chg"]))
        sector["rank"] = _safe_int(_record_get(matched, ["排名", "rank"]))
        sector["rank_total"] = len(board_records) if board_records else None
    elif not industry:
        _append_warning(warnings, "sector: 缺少行业信息，无法匹配板块")
    elif not board_records:
        _append_warning(warnings, "sector: 行业板块列表接口失败")
    else:
        _append_warning(warnings, "sector: 未在行业板块列表中找到 %s" % industry)

    flow_raw = _call_akshare(
        "sector",
        [
            ("stock_sector_fund_flow_rank", {"indicator": "今日", "sector_type": "行业资金流"}),
            ("stock_board_industry_fund_flow_rank_em", {"symbol": "今日"}),
        ],
        warnings,
        ak_module=ak_module,
        warn=False,
    )
    if industry:
        for record in _records(flow_raw):
            name = _safe_str(_record_get(record, ["名称", "板块名称", "行业名称", "name"]))
            if name and (name == industry or industry in name or name in industry):
                sector["main_net_inflow"] = _safe_float(_record_get(record, [
                    "今日主力净流入-净额", "主力净流入-净额", "主力净流入",
                    "main_net_inflow",
                ]))
                break

    missing = [key for key, value in sector.items() if value is None]
    if missing:
        _append_warning(warnings, "sector: 字段缺失（%s）" % "、".join(missing))
    return sector


# ---------------------------------------------------------------------------
# Market
# ---------------------------------------------------------------------------


def empty_market() -> Dict[str, Any]:
    return {
        "indexes": [
            {"name": name, "close": None, "pct_chg": None}
            for name, _symbol in _INDEX_DEFS
        ],
        "up_count": None,
        "down_count": None,
        "northbound_net": None,
        "margin_balance": None,
    }


def fetch_market(as_of: Optional[str] = None,
                 warnings: Optional[List[str]] = None,
                 ak_module: Optional[Any] = None) -> Dict[str, Any]:
    warnings = warnings if warnings is not None else []
    as_of_date = _parse_date(as_of) or _china_now().date()
    as_of_iso = as_of_date.isoformat()
    start_date = as_of_date - timedelta(days=40)
    start_compact = start_date.strftime("%Y%m%d")
    end_compact = as_of_date.strftime("%Y%m%d")

    indexes: List[Dict[str, Any]] = []
    missing_indexes: List[str] = []
    for name, symbol in _INDEX_DEFS:
        raw = _call_akshare(
            "market",
            [
                # Confirmed working in akshare 1.18.88.
                ("stock_zh_index_daily", {"symbol": symbol}),
                (
                    "stock_zh_index_daily_em",
                    {
                        "symbol": symbol,
                        "start_date": start_compact,
                        "end_date": end_compact,
                    },
                ),
            ],
            warnings,
            ak_module=ak_module,
            warn=False,
        )
        rows: List[Tuple[str, Optional[float], Any]] = []
        for record in _records(raw):
            trade_date = _norm_date(_record_get(record, ["date", "日期", "trade_date"]))
            if not trade_date or trade_date > as_of_iso:
                continue
            rows.append((
                trade_date,
                _safe_float(_record_get(record, ["close", "收盘"])),
                _record_get(record, ["涨跌幅", "pct_chg"]),
            ))
        rows.sort(key=lambda item: item[0])

        close = None
        pct_chg = None
        if rows:
            close = rows[-1][1]
            raw_pct = rows[-1][2]
            if raw_pct is not None:
                pct_chg = _percent_value(raw_pct)
            elif len(rows) >= 2 and rows[-2][1] not in (None, 0) and close is not None:
                pct_chg = (close - rows[-2][1]) / rows[-2][1]
        if close is None:
            missing_indexes.append(name)
        indexes.append({
            "name": name,
            "close": _round_num(close, 2),
            "pct_chg": _round_num(pct_chg, 6),
        })
    if missing_indexes:
        _append_warning(warnings, "market: 指数数据缺失（%s）" % "、".join(missing_indexes))

    up_count = None
    down_count = None
    activity = _call_akshare(
        "market",
        [("stock_market_activity_legu", {})],
        warnings,
        ak_module=ak_module,
        warn=False,
    )
    activity_records = _records(activity)
    if activity_records:
        activity_items = _item_value_map(activity)
        up_count = _safe_int(_map_get(activity_items, ["上涨", "上涨家数", "up_count"]))
        down_count = _safe_int(_map_get(activity_items, ["下跌", "下跌家数", "down_count"]))
        if (up_count is not None or down_count is not None) and as_of_iso != _today_iso():
            _append_warning(warnings, "market: 涨跌家数为最新快照，非 as_of 当日")

    if up_count is None or down_count is None:
        spot = _call_akshare(
            "market",
            [("stock_zh_a_spot_em", {}), ("stock_zh_a_spot", {})],
            warnings,
            ak_module=ak_module,
            warn=False,
        )
        records = _records(spot)
        if records:
            ups = downs = found = 0
            for record in records:
                raw_pct = _safe_float(_record_get(record, ["涨跌幅", "pct_chg"]))
                if raw_pct is None:
                    continue
                found += 1
                if raw_pct > 0:
                    ups += 1
                elif raw_pct < 0:
                    downs += 1
            if found:
                up_count, down_count = ups, downs
        if as_of_iso != _today_iso():
            _append_warning(warnings, "market: 涨跌家数使用最新快照，非 as_of 当日")
    if up_count is None or down_count is None:
        _append_warning(warnings, "market: 涨跌家数缺失")

    northbound_net = None
    north_raw = _call_akshare(
        "market",
        [
            ("stock_hsgt_fund_flow_summary_em", {}),
            ("stock_hsgt_north_net_flow_in_em", {"symbol": "北上"}),
            ("stock_hsgt_hist_em", {"symbol": "北向资金"}),
        ],
        warnings,
        ak_module=ak_module,
        warn=False,
    )
    north_records = _records(north_raw)
    north_zero_warned = False
    if north_records:
        has_net_buy = any(
            _record_get(record, ["成交净买额", "净买额", "当日资金流入"]) is not None
            for record in north_records
        )
        if has_net_buy:
            values: List[float] = []
            for record in north_records:
                direction = _safe_str(_record_get(record, ["资金方向", "方向", "类型"])) or ""
                board = _safe_str(_record_get(record, ["板块", "类型名称"])) or ""
                if "南向" in direction:
                    continue
                if direction and "北向" not in direction and not any(
                    token in board for token in ("沪股通", "深股通")
                ):
                    continue
                value = _safe_float(_record_get(record, [
                    "成交净买额", "净买额", "当日资金流入",
                ]))
                if value is not None:
                    values.append(value)
            if values:
                if all(value == 0 for value in values):
                    # Many endpoints now return an all-zero placeholder after
                    # northbound daily net-flow disclosure was discontinued.
                    _append_warning(
                        warnings,
                        "market: 北向资金接口仅返回 0，疑似停止披露/占位，已置 null",
                    )
                    north_zero_warned = True
                else:
                    total = sum(values)
                    northbound_net = total * 1e8  # summary is reported in 亿元
                    if total == 0:
                        _append_warning(
                            warnings,
                            "market: 北向资金净额合计为 0（多空抵消），按接口返回值记录",
                        )
        else:
            rows: List[Tuple[str, float]] = []
            for record in north_records:
                trade_date = _norm_date(_record_get(record, [
                    "date", "日期", "trade_date", "交易日",
                ]))
                value = _safe_float(_record_get(record, [
                    "value", "当日成交净买额", "净买额", "成交净买额",
                ]))
                if trade_date and trade_date <= as_of_iso and value is not None:
                    rows.append((trade_date, value))
            if rows:
                rows.sort(key=lambda item: item[0])
                latest_value = rows[-1][1]
                if latest_value == 0 and all(value == 0 for _day, value in rows):
                    _append_warning(
                        warnings,
                        "market: 北向资金接口仅返回 0，疑似停止披露/占位，已置 null",
                    )
                    north_zero_warned = True
                else:
                    northbound_net = latest_value * 1e8
                    if latest_value == 0:
                        _append_warning(
                            warnings,
                            "market: 北向资金接口明确返回 0，按 0 记录",
                        )
    if northbound_net is None and not north_zero_warned:
        _append_warning(warnings, "market: 北向资金净流入缺失（接口可能已停止披露）")

    margin_balance = None
    sse_balance = None
    szse_balance = None
    sse_raw = _call_akshare(
        "market",
        [("stock_margin_sse", {
            "start_date": (as_of_date - timedelta(days=10)).strftime("%Y%m%d"),
            "end_date": end_compact,
        })],
        warnings,
        ak_module=ak_module,
        warn=False,
    )
    sse_rows: List[Tuple[str, Optional[float]]] = []
    for record in _records(sse_raw):
        trade_date = _norm_date(_record_get(record, [
            "信用交易日期", "交易日期", "日期", "date", "trade_date",
        ]))
        if trade_date and trade_date <= as_of_iso:
            sse_rows.append((trade_date, _safe_float(_record_get(record, [
                "融资余额", "margin_balance", "rzrqye",
            ]))))
    sse_rows.sort(key=lambda item: item[0])
    if sse_rows:
        sse_balance = sse_rows[-1][1]

    szse_raw = _call_akshare(
        "market",
        [("stock_margin_szse", {"date": end_compact})],
        warnings,
        ak_module=ak_module,
        warn=False,
    )
    szse_rows = []
    for record in _records(szse_raw):
        trade_date = _norm_date(_record_get(record, [
            "信用交易日期", "交易日期", "日期", "date", "trade_date",
        ]))
        if trade_date is None or trade_date <= as_of_iso:
            szse_rows.append((trade_date or as_of_iso, _safe_float(_record_get(record, [
                "融资余额", "margin_balance", "rzrqye",
            ]))))
    szse_rows.sort(key=lambda item: item[0])
    if szse_rows:
        szse_balance = szse_rows[-1][1]

    if sse_balance is None and szse_balance is None:
        _append_warning(warnings, "market: 融资余额缺失")
    elif sse_balance is None or szse_balance is None:
        _append_warning(warnings, "market: 仅获取到部分融资余额（缺交易所数据）")
    else:
        margin_balance = sse_balance + szse_balance

    return {
        "indexes": indexes,
        "up_count": up_count,
        "down_count": down_count,
        "northbound_net": _round_num(northbound_net, 2),
        "margin_balance": _round_num(margin_balance, 2),
    }


# ---------------------------------------------------------------------------
# Trading constraints / price limits
# ---------------------------------------------------------------------------


def _price_limit_pct(board: str, is_st: bool) -> float:
    if board == "bse":
        return 0.3
    if is_st:
        return 0.05
    if board in ("star", "chinext"):
        return 0.2
    return 0.1


def empty_constraints(board: str = "main", is_st: bool = False) -> Dict[str, Any]:
    return {
        "suspended": False,
        "board": board,
        "price_limit_pct": _price_limit_pct(board, is_st),
        "limit_up_price": None,
        "limit_down_price": None,
        "last_trade_date": None,
    }


def fetch_constraints(quotes: Optional[Dict[str, Any]],
                      basic: Optional[Dict[str, Any]] = None,
                      symbol: Any = "600519",
                      warnings: Optional[List[str]] = None,
                      as_of: Optional[str] = None,
                      tencent_fetcher: Optional[Callable[[], Any]] = None
                      ) -> Dict[str, Any]:
    warnings = warnings if warnings is not None else []
    info = _info(symbol)
    board = info["board"]
    is_st = _safe_bool((basic or {}).get("is_st"))
    limit_pct = _price_limit_pct(board, is_st)

    rows = (quotes or {}).get("rows") or []
    last = rows[-1] if rows else None
    close = _safe_float((last or {}).get("close"))
    last_trade_date = _safe_str((last or {}).get("date"))
    volume = _safe_float((last or {}).get("volume"))

    suspended = bool(volume == 0)
    if suspended:
        _append_warning(warnings, "constraints: 最新交易日成交量为 0，可能停牌")

    if last_trade_date and as_of:
        last_date = _parse_date(last_trade_date)
        as_of_date = _parse_date(as_of)
        if last_date and as_of_date and (as_of_date - last_date).days > 10:
            # Stale data may be a long holiday rather than a suspension; keep
            # ``suspended`` tied to actual zero-volume evidence.
            _append_warning(
                warnings,
                "constraints: 最近交易日 %s 距 as_of %s 超过 10 天，可能停牌或数据滞后"
                % (last_trade_date, as_of),
            )

    limit_up = None
    limit_down = None
    if close is not None and close > 0:
        limit_up = _round_half_up(Decimal(str(close)) * (Decimal("1") + Decimal(str(limit_pct))), 2)
        limit_down = _round_half_up(Decimal(str(close)) * (Decimal("1") - Decimal(str(limit_pct))), 2)

    # Tencent exposes the exchange's actual limit prices; prefer them over the
    # qfq-derived estimate when available.
    snapshot = _tencent_lookup(info, warnings, tencent_fetcher)
    if snapshot:
        snapshot_date = _safe_str(snapshot.get("trade_date"))
        date_aligned = (
            not last_trade_date
            or not snapshot_date
            or snapshot_date == last_trade_date
        )
        if not date_aligned:
            _append_warning(
                warnings,
                "constraints: 腾讯快照日期 %s 与最近交易日 %s 不一致，涨跌停改用行情估算"
                % (snapshot_date, last_trade_date),
            )
        else:
            tencent_up = _safe_float(snapshot.get("limit_up_price"))
            tencent_down = _safe_float(snapshot.get("limit_down_price"))
            if tencent_up is not None or tencent_down is not None:
                if tencent_up is not None:
                    limit_up = _round_num(tencent_up, 2)
                if tencent_down is not None:
                    limit_down = _round_num(tencent_down, 2)
                _append_warning(
                    warnings,
                    "constraints: 涨跌停参考价来自腾讯行情快照（交易所实时口径）",
                )

    if limit_up is None and limit_down is None:
        _append_warning(warnings, "constraints: 缺少最新收盘价，无法计算涨跌停参考价")

    return {
        "suspended": suspended,
        "board": board,
        "price_limit_pct": limit_pct,
        "limit_up_price": limit_up,
        "limit_down_price": limit_down,
        "last_trade_date": last_trade_date,
    }


# ---------------------------------------------------------------------------
# Bundle assembly
# ---------------------------------------------------------------------------


def empty_technical() -> Dict[str, Any]:
    return {
        "as_of": None,
        "last": {
            "close": None,
            "ma5": None, "ma10": None, "ma20": None, "ma60": None,
            "ema12": None, "ema26": None,
            "rsi14": None,
            "macd": None, "macd_signal": None, "macd_hist": None,
            "boll_upper": None, "boll_mid": None, "boll_lower": None,
            "atr14": None, "kdj_k": None, "kdj_d": None, "kdj_j": None,
            "volume_ratio": None,
        },
        "series": {"dates": [], "close": [], "ma20": [], "rsi14": [], "macd_hist": []},
        "signals": {
            "trend": "unknown",
            "ma_alignment": "unknown",
            "rsi_state": "unknown",
            "macd_state": "unknown",
            "volume_state": "unknown",
        },
    }


def collect_bundle(symbol: Any, as_of: Optional[str] = None,
                   days: int = DEFAULT_DAYS, start: Optional[str] = None,
                   source: str = "akshare",
                   warnings: Optional[List[str]] = None,
                   ak_module: Optional[Any] = None,
                   fetchers: Optional[Dict[str, Callable[[], Any]]] = None) -> Dict[str, Any]:
    """Fetch every module independently and assemble a CONTRACT bundle.

    ``fetchers`` is an optional test hook: a mapping of module name to a
    zero-argument callable.  Production callers should leave it empty.
    """
    warnings = warnings if warnings is not None else []
    info = resolve_symbol(symbol)
    as_of_iso = _norm_date(as_of) or _today_iso()
    source = (source or "akshare").lower()
    if source not in {"akshare", "tushare"}:
        raise ValueError("不支持的 --source: %s" % source)
    if source == "tushare":
        _append_warning(
            warnings,
            "source: 当前构建未启用 tushare 后端（缺少依赖/token），已回退 akshare",
        )
        source = "akshare"

    overrides = fetchers or {}

    def run(name: str, default_func: Callable[[], Any], default: Any) -> Any:
        func = overrides.get(name, default_func)
        return safe_call(name, func, warnings, default)

    # Fetch the Tencent snapshot at most once per bundle, and only when an
    # Eastmoney endpoint actually fails / lacks a field.
    tencent_cache: Dict[str, Any] = {"loaded": False, "value": None}

    def tencent_fetcher() -> Optional[Dict[str, Any]]:
        if not tencent_cache["loaded"]:
            tencent_cache["value"] = fetch_tencent_snapshot(info, warnings=warnings)
            tencent_cache["loaded"] = True
        value = tencent_cache["value"]
        return value if isinstance(value, dict) else None

    quotes = run(
        "quotes",
        lambda: fetch_quotes(
            info, as_of=as_of_iso, days=days, start=start,
            warnings=warnings, ak_module=ak_module,
        ),
        None,
    )
    if not isinstance(quotes, dict) or not quotes.get("rows"):
        raise FatalDataError("无法获取行情数据（quotes 为空）")

    basic = run(
        "basic",
        lambda: fetch_basic(
            info, warnings=warnings, ak_module=ak_module,
            tencent_fetcher=tencent_fetcher,
        ),
        empty_basic(),
    )
    if not isinstance(basic, dict):
        basic = empty_basic()
    name = basic.pop("name", None)
    basic = {
        "industry": basic.get("industry"),
        "list_date": basic.get("list_date"),
        "is_st": _safe_bool(basic.get("is_st")),
        "market_cap": basic.get("market_cap"),
        "float_market_cap": basic.get("float_market_cap"),
        "total_shares": basic.get("total_shares"),
        "float_shares": basic.get("float_shares"),
    }

    valuation = run(
        "valuation",
        lambda: fetch_valuation(
            info, basic=basic, warnings=warnings, ak_module=ak_module,
            as_of=as_of_iso, tencent_fetcher=tencent_fetcher,
        ),
        empty_valuation(),
    )
    finance = run(
        "finance",
        lambda: fetch_financials(info, as_of=as_of_iso, warnings=warnings, ak_module=ak_module),
        empty_financials(),
    )
    news = run(
        "news",
        lambda: fetch_news(info, as_of=as_of_iso, warnings=warnings, ak_module=ak_module),
        [],
    )
    sector = run(
        "sector",
        lambda: fetch_sector(info, basic=basic, warnings=warnings, ak_module=ak_module),
        empty_sector(),
    )
    market = run(
        "market",
        lambda: fetch_market(as_of=as_of_iso, warnings=warnings, ak_module=ak_module),
        empty_market(),
    )
    constraints = run(
        "constraints",
        lambda: fetch_constraints(
            quotes, basic=basic, symbol=info, warnings=warnings, as_of=as_of_iso,
            tencent_fetcher=tencent_fetcher,
        ),
        empty_constraints(info["board"], _safe_bool(basic.get("is_st"))),
    )

    if not isinstance(finance, dict):
        finance = empty_financials()
    fundamentals = finance.get("fundamentals") if isinstance(finance.get("fundamentals"), dict) else {"periods": []}
    financials = finance.get("financials") if isinstance(finance.get("financials"), dict) else empty_financials()["financials"]

    bundle = {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "symbol": info["code"],
            "name": name or "",
            "exchange": info["exchange"],
            "as_of": as_of_iso,
            "source": source,
            "fetched_at": _now_iso(),
            "currency": "CNY",
            "warnings": warnings,
        },
        "basic": basic,
        "quotes": quotes,
        "valuation": valuation,
        "fundamentals": fundamentals,
        "financials": financials,
        "news": news if isinstance(news, list) else [],
        "sector": sector if isinstance(sector, dict) else empty_sector(),
        "market": market if isinstance(market, dict) else empty_market(),
        "constraints": constraints if isinstance(constraints, dict) else empty_constraints(info["board"]),
        "technical": empty_technical(),
    }
    return bundle


# ---------------------------------------------------------------------------
# Offline fixture / CLI
# ---------------------------------------------------------------------------


def _load_offline_bundle(path: str, requested_symbol: Optional[str] = None,
                         as_of: Optional[str] = None,
                         warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    warnings = warnings if warnings is not None else []
    fixture = Path(path)
    if not fixture.exists():
        raise FatalDataError("offline fixture 不存在: %s" % fixture)
    try:
        data = json.loads(fixture.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FatalDataError("offline fixture 读取失败: %s" % _short_exc(exc))
    if not isinstance(data, dict):
        raise FatalDataError("offline fixture 必须是 JSON 对象")

    meta = data.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        data["meta"] = meta

    fixture_warnings = meta.get("warnings")
    if not isinstance(fixture_warnings, list):
        fixture_warnings = []
    fixture_warnings = [str(item) for item in fixture_warnings]
    warnings.extend(item for item in fixture_warnings if item not in warnings)

    if requested_symbol:
        requested = resolve_symbol(requested_symbol)["code"]
        fixture_symbol = _safe_str(meta.get("symbol"))
        if fixture_symbol and fixture_symbol != requested:
            _append_warning(
                warnings,
                "offline: fixture 代码 %s 与请求 %s 不一致" % (fixture_symbol, requested),
            )
        meta.setdefault("symbol", requested)
    if not meta.get("symbol"):
        meta["symbol"] = resolve_symbol(requested_symbol)["code"] if requested_symbol else ""
    if meta.get("symbol"):
        try:
            meta.setdefault("exchange", resolve_symbol(meta["symbol"])["exchange"])
        except ValueError:
            pass
    meta.setdefault("as_of", _norm_date(as_of) or _today_iso())
    meta.setdefault("currency", "CNY")

    # Offline output is always marked as fixture-sourced so downstream tools
    # cannot mistake it for live data.
    meta["source"] = "fixture"
    meta["fetched_at"] = _now_iso()
    meta["warnings"] = list(warnings)
    return data


def _default_output(symbol_code: str, as_of_iso: str) -> Path:
    return Path("data") / symbol_code / as_of_iso / "bundle.json"


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    payload = _sanitize(data)
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch A-share research data into a CONTRACT.md bundle."
    )
    parser.add_argument("--symbol", required=True, help="6位代码，如 600519 或 600519.SH")
    parser.add_argument("--as-of", default=None, help="分析日期 YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help="行情回看自然日数（默认 400）")
    parser.add_argument("--start", default=None, help="行情起始日期 YYYY-MM-DD")
    parser.add_argument("--out", default=None, help="输出路径")
    parser.add_argument("--source", choices=["akshare", "tushare"], default="akshare")
    parser.add_argument("--offline", default=None, help="离线 fixture 路径")
    parser.add_argument("--strict", action="store_true",
                        help="有任何 warning 时以退出码 2 结束")
    args = parser.parse_args(argv)

    warnings: List[str] = []
    try:
        if args.offline:
            bundle = _load_offline_bundle(
                args.offline,
                requested_symbol=args.symbol,
                as_of=args.as_of,
                warnings=warnings,
            )
            meta = bundle.get("meta", {})
            symbol_code = _safe_str(meta.get("symbol")) or resolve_symbol(args.symbol)["code"]
            as_of_iso = _safe_str(meta.get("as_of")) or _norm_date(args.as_of) or _today_iso()
        else:
            info = resolve_symbol(args.symbol)
            symbol_code = info["code"]
            as_of_iso = _norm_date(args.as_of) or _today_iso()
            bundle = collect_bundle(
                info,
                as_of=as_of_iso,
                days=args.days,
                start=args.start,
                source=args.source,
                warnings=warnings,
            )
    except FatalDataError as exc:
        print("ERROR %s" % exc, file=sys.stderr)
        return 3
    except (ValueError, OSError) as exc:
        print("ERROR %s" % _short_exc(exc), file=sys.stderr)
        return 3

    meta = bundle.setdefault("meta", {})
    if not isinstance(meta, dict):
        meta = {}
        bundle["meta"] = meta
    meta["warnings"] = list(warnings)

    out_path = Path(args.out) if args.out else _default_output(symbol_code, as_of_iso)
    try:
        _write_json(out_path, bundle)
    except (OSError, TypeError, ValueError) as exc:
        print("ERROR 写入失败: %s" % _short_exc(exc), file=sys.stderr)
        return 3

    print("WROTE %s" % out_path)
    if warnings:
        # CONTRACT calls this "partial success".  --strict exists so CI can
        # make the same signal explicit; both paths use exit code 2.
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
