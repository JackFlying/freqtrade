#!/usr/bin/env python3

import asyncio
import csv
import json
import math
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode
from urllib.request import urlopen

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from freqtrade.configuration.load_config import load_from_files
from pydantic import BaseModel, Field


ROOT_DIR = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "static"
CONFIG_PATH = ROOT_DIR / "user_data/config_scan.json"
RESULTS_DIR = ROOT_DIR / "user_data/scan_results"
CANDIDATES_PATH = RESULTS_DIR / "daily_trend_candidates.csv"
STATUS_PATH = RESULTS_DIR / "daily_trend_status.json"
PREVIEW_RESULTS_DIR = RESULTS_DIR / "preview"
PREVIEW_CANDIDATES_PATH = PREVIEW_RESULTS_DIR / "daily_trend_candidates.csv"
PREVIEW_STATUS_PATH = PREVIEW_RESULTS_DIR / "daily_trend_status.json"
PREVIEW_SETTINGS_PATH = PREVIEW_RESULTS_DIR / "settings.json"
BACKTEST_RESULTS_DIR = ROOT_DIR / "user_data/backtest_results"
BACKTEST_RESULT_PATH = BACKTEST_RESULTS_DIR / "latest.json"
BACKTEST_STATUS_PATH = BACKTEST_RESULTS_DIR / "status.json"
BACKTEST_4H_RAW_PATH = BACKTEST_RESULTS_DIR / "research_4h_latest.json"
ALLOWED_TIMEFRAMES = {"15m", "1h", "4h", "1d"}
OHLCV_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
MA_PERIODS = (7, 20, 99)
SCREENING_SETTING_KEYS = {
    "min_change_20d",
    "max_change_20d",
    "max_drawdown_to_gain_ratio_pct",
    "lookback_days",
    "use_4h_ma_filter",
    "use_ma99_filter",
}

app = FastAPI(title="Freqtrade Strategy Console", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def revalidate_static_assets(request, call_next):
    # Static assets are referenced without a version query string, so instruct
    # browsers to revalidate via ETag on every load. This keeps the served
    # HTML/JS/CSS in sync after each deploy instead of serving stale cached
    # copies, while still allowing 304 responses when nothing changed.
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


_response_cache: dict[tuple[str, str, int], tuple[float, dict[str, Any]]] = {}
# Daily candles come from the local feather cache and change at most once a day,
# so they can be cached longer. Intraday candles are fetched live from Binance;
# their recent history barely changes, so a modest cache spares repeated network
# round trips when the user toggles timeframes or re-selects a pair.
_CANDLE_CACHE_TTL = {"1d": 300.0}
_CANDLE_CACHE_TTL_DEFAULT = 60.0
# Validating the requested pair against the candidate list would otherwise
# re-parse the CSV on every /api/candles call. Cache it briefly instead.
_candidate_pairs_cache: tuple[float, set[str]] | None = None
_CANDIDATE_PAIRS_TTL = 10.0
_scan_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "return_code": None,
    "message": "尚未手动扫描",
}
_scan_lock = asyncio.Lock()
_backtest_lock = asyncio.Lock()
_backtest_process: asyncio.subprocess.Process | None = None
_daily_open_locks: dict[str, asyncio.Lock] = {}


class DashboardSettings(BaseModel):
    strategy_timeframe: Literal["1d", "4h"] = "1d"
    max_open_trades: int = Field(default=1, ge=1, le=4)
    min_change_20d: float = Field(ge=-100, le=10000)
    max_change_20d: float = Field(ge=-100, le=10000)
    max_drawdown_to_gain_ratio_pct: float = Field(default=50.0, ge=0, le=100)
    lookback_days: int = Field(default=20, ge=2, le=364)
    use_4h_ma_filter: bool = False
    use_ma99_filter: bool = False
    ma7_reclaim_enabled: bool = True
    ma7_reclaim_tolerance_pct: float = Field(default=1.0, ge=0.1, le=5)
    ma7_reclaim_lookback_days: int = Field(default=2, ge=1, le=5)
    ma7_exit_threshold_pct: float = Field(default=2.0, ge=0, le=100)
    hard_stoploss_pct: float = Field(default=6.0, ge=0.1, le=99)
    peak_drawdown_stop_enabled: bool = False
    peak_drawdown_stop_pct: float = Field(default=5.0, ge=0.1, le=99)
    dynamic_drawdown_stop_enabled: bool = False
    dynamic_drawdown_activation_pct: float = Field(default=3.0, ge=0.1, le=100)
    dynamic_max_profit_giveback_pct: float = Field(default=5.0, ge=0.5, le=50)
    chandelier_exit_enabled: bool = False
    partial_take_profit_enabled: bool = False
    no_progress_exit_enabled: bool = False
    candidate_reentry_required: bool = False
    cooldown_enabled: bool = False
    cooldown_hours: float = Field(default=4.0, ge=0.1, le=168)
    entry_enabled: bool = True
    candidate_scan_interval_seconds: int = Field(default=900, ge=60, le=86400)


class BacktestRequest(BaseModel):
    days: int = Field(default=30, ge=7, le=540)
    initial_balance: float = Field(default=1000.0, ge=100, le=10000000)
    strategy_timeframe: Literal["1d", "4h"] = "1d"


def read_config() -> dict[str, Any]:
    private_config_path = ROOT_DIR / "user_data/config_private.json"
    return load_from_files([str(CONFIG_PATH), str(private_config_path)])


def parse_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def preview_active() -> bool:
    return PREVIEW_CANDIDATES_PATH.exists() and PREVIEW_STATUS_PATH.exists()


def read_candidates(path: Path | None = None) -> list[dict[str, Any]]:
    candidate_path = path or (
        PREVIEW_CANDIDATES_PATH if preview_active() else CANDIDATES_PATH
    )
    if not candidate_path.exists():
        return []

    candidates = []
    with candidate_path.open(encoding="utf-8", newline="") as csv_file:
        for row in csv.DictReader(csv_file):
            candidates.append(
                {
                    "rank": int(row["rank"]),
                    "symbol": row["symbol"],
                    "pair": row["pair"],
                    "price": parse_number(row["price"]),
                    "ma_7": parse_number(row.get("ma_7")),
                    "ma_20": parse_number(row.get("ma_20")),
                    "ma_99": parse_number(row.get("ma_99")),
                    "change_20d": parse_number(row.get("change_20d")),
                    "drawdown_20d": parse_number(row.get("drawdown_20d")),
                    "quote_volume_24h": parse_number(row["quote_volume_24h"]),
                    "completed_daily_candles": int(row["completed_daily_candles"]),
                    "is_provisional_daily_candle": (
                        row["is_provisional_daily_candle"].lower() == "true"
                    ),
                    "updated_at": row["updated_at"],
                }
            )
    return candidates


def candidate_pairs_cached() -> set[str]:
    global _candidate_pairs_cache
    now = time.monotonic()
    if _candidate_pairs_cache and now - _candidate_pairs_cache[0] < _CANDIDATE_PAIRS_TTL:
        return _candidate_pairs_cache[1]
    pairs = {item["pair"] for item in read_candidates()}
    _candidate_pairs_cache = (now, pairs)
    return pairs


def read_status(path: Path | None = None) -> dict[str, Any]:
    status_path = path or (PREVIEW_STATUS_PATH if preview_active() else STATUS_PATH)
    if not status_path.exists():
        return {}
    return json.loads(status_path.read_text(encoding="utf-8"))


def runtime_settings_path() -> Path:
    config = read_config()
    path = (
        ROOT_DIR
        / config["scanner"]["daily_trend"]["cache_directory"]
        / "runtime_settings.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def read_runtime_settings() -> dict[str, Any]:
    scanner_config = read_config()["scanner"]["daily_trend"]
    default_interval = int(scanner_config["update_interval_seconds"])
    defaults = {
        "strategy_timeframe": "1d",
        "max_open_trades": 1,
        "min_change_20d": 5.0,
        "max_change_20d": 100.0,
        "max_drawdown_to_gain_ratio_pct": 50.0,
        "lookback_days": 20,
        "use_4h_ma_filter": False,
        "use_ma99_filter": False,
        "ma7_reclaim_enabled": True,
        "ma7_reclaim_tolerance_pct": 1.0,
        "ma7_reclaim_lookback_days": 2,
        "ma7_exit_threshold_pct": 2.0,
        "hard_stoploss_pct": 6.0,
        "peak_drawdown_stop_enabled": False,
        "peak_drawdown_stop_pct": 5.0,
        "dynamic_drawdown_stop_enabled": False,
        "dynamic_drawdown_activation_pct": 3.0,
        "dynamic_max_profit_giveback_pct": 5.0,
        "chandelier_exit_enabled": False,
        "partial_take_profit_enabled": False,
        "no_progress_exit_enabled": False,
        "candidate_reentry_required": False,
        "cooldown_enabled": False,
        "cooldown_hours": 4.0,
        "entry_enabled": True,
        "candidate_scan_interval_seconds": default_interval,
    }
    path = runtime_settings_path()
    if not path.exists():
        return defaults
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        stored_timeframe = payload.get("strategy_timeframe")
        if stored_timeframe not in {"1d", "4h"}:
            # Older runtime files did not persist the selector. The 4h MA
            # filter is the unambiguous marker for those saved 4h presets.
            stored_timeframe = (
                "4h" if payload.get("use_4h_ma_filter") is True else "1d"
            )
        return {
            "strategy_timeframe": stored_timeframe,
            "max_open_trades": max(
                1,
                min(4, int(payload.get("max_open_trades", defaults["max_open_trades"]))),
            ),
            "min_change_20d": float(
                payload.get("min_change_20d", defaults["min_change_20d"])
            ),
            "max_change_20d": float(
                payload.get("max_change_20d", defaults["max_change_20d"])
            ),
            "max_drawdown_to_gain_ratio_pct": float(
                payload.get(
                    "max_drawdown_to_gain_ratio_pct",
                    defaults["max_drawdown_to_gain_ratio_pct"],
                )
            ),
            "lookback_days": int(
                payload.get("lookback_days", defaults["lookback_days"])
            ),
            "use_4h_ma_filter": bool(
                payload.get(
                    "use_4h_ma_filter",
                    defaults["use_4h_ma_filter"],
                )
            ),
            "use_ma99_filter": bool(
                payload.get(
                    "use_ma99_filter",
                    defaults["use_ma99_filter"],
                )
            ),
            "ma7_reclaim_enabled": bool(
                payload.get(
                    "ma7_reclaim_enabled",
                    defaults["ma7_reclaim_enabled"],
                )
            ),
            "ma7_reclaim_tolerance_pct": max(
                0.1,
                min(
                    5.0,
                    float(
                        payload.get(
                            "ma7_reclaim_tolerance_pct",
                            defaults["ma7_reclaim_tolerance_pct"],
                        )
                    ),
                ),
            ),
            "ma7_reclaim_lookback_days": max(
                1,
                min(
                    5,
                    int(
                        payload.get(
                            "ma7_reclaim_lookback_days",
                            defaults["ma7_reclaim_lookback_days"],
                        )
                    ),
                ),
            ),
            "ma7_exit_threshold_pct": float(
                payload.get(
                    "ma7_exit_threshold_pct",
                    defaults["ma7_exit_threshold_pct"],
                )
            ),
            "hard_stoploss_pct": float(
                payload.get(
                    "hard_stoploss_pct",
                    defaults["hard_stoploss_pct"],
                )
            ),
            "peak_drawdown_stop_enabled": bool(
                payload.get(
                    "peak_drawdown_stop_enabled",
                    defaults["peak_drawdown_stop_enabled"],
                )
            ),
            "peak_drawdown_stop_pct": float(
                payload.get(
                    "peak_drawdown_stop_pct",
                    defaults["peak_drawdown_stop_pct"],
                )
            ),
            "dynamic_drawdown_stop_enabled": bool(
                payload.get(
                    "dynamic_drawdown_stop_enabled",
                    defaults["dynamic_drawdown_stop_enabled"],
                )
            ),
            "dynamic_drawdown_activation_pct": float(
                payload.get(
                    "dynamic_drawdown_activation_pct",
                    defaults["dynamic_drawdown_activation_pct"],
                )
            ),
            "dynamic_max_profit_giveback_pct": max(
                0.5,
                min(
                    50.0,
                    float(
                        payload.get(
                            "dynamic_max_profit_giveback_pct",
                            defaults["dynamic_max_profit_giveback_pct"],
                        )
                    ),
                ),
            ),
            "chandelier_exit_enabled": bool(
                payload.get(
                    "chandelier_exit_enabled",
                    defaults["chandelier_exit_enabled"],
                )
            ),
            "partial_take_profit_enabled": bool(
                payload.get(
                    "partial_take_profit_enabled",
                    defaults["partial_take_profit_enabled"],
                )
            ),
            "no_progress_exit_enabled": bool(
                payload.get(
                    "no_progress_exit_enabled",
                    defaults["no_progress_exit_enabled"],
                )
            ),
            "candidate_reentry_required": bool(
                payload.get(
                    "candidate_reentry_required",
                    defaults["candidate_reentry_required"],
                )
            ),
            "cooldown_enabled": bool(
                payload.get("cooldown_enabled", defaults["cooldown_enabled"])
            ),
            "cooldown_hours": max(
                0.1,
                min(
                    168.0,
                    float(
                        payload.get(
                            "cooldown_hours",
                            float(payload.get("cooldown_minutes", 240)) / 60,
                        )
                    ),
                ),
            ),
            "entry_enabled": bool(
                payload.get("entry_enabled", defaults["entry_enabled"])
            ),
            "candidate_scan_interval_seconds": max(
                60,
                min(
                    86400,
                    int(
                        payload.get(
                            "candidate_scan_interval_seconds",
                            defaults["candidate_scan_interval_seconds"],
                        )
                    ),
                ),
            ),
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return defaults


def write_runtime_settings(
    settings: DashboardSettings,
) -> dict[str, Any]:
    path = runtime_settings_path()
    payload = {
        "strategy_timeframe": settings.strategy_timeframe,
        "max_open_trades": settings.max_open_trades,
        "min_change_20d": settings.min_change_20d,
        "max_change_20d": settings.max_change_20d,
        "max_drawdown_to_gain_ratio_pct": settings.max_drawdown_to_gain_ratio_pct,
        "lookback_days": settings.lookback_days,
        "use_4h_ma_filter": settings.use_4h_ma_filter,
        "use_ma99_filter": settings.use_ma99_filter,
        "ma7_reclaim_enabled": settings.ma7_reclaim_enabled,
        "ma7_reclaim_tolerance_pct": settings.ma7_reclaim_tolerance_pct,
        "ma7_reclaim_lookback_days": settings.ma7_reclaim_lookback_days,
        "ma7_exit_threshold_pct": settings.ma7_exit_threshold_pct,
        "hard_stoploss_pct": settings.hard_stoploss_pct,
        "peak_drawdown_stop_enabled": settings.peak_drawdown_stop_enabled,
        "peak_drawdown_stop_pct": settings.peak_drawdown_stop_pct,
        "dynamic_drawdown_stop_enabled": settings.dynamic_drawdown_stop_enabled,
        "dynamic_drawdown_activation_pct": settings.dynamic_drawdown_activation_pct,
        "dynamic_max_profit_giveback_pct": settings.dynamic_max_profit_giveback_pct,
        "chandelier_exit_enabled": settings.chandelier_exit_enabled,
        "partial_take_profit_enabled": settings.partial_take_profit_enabled,
        "no_progress_exit_enabled": settings.no_progress_exit_enabled,
        "candidate_reentry_required": settings.candidate_reentry_required,
        "cooldown_enabled": settings.cooldown_enabled,
        "cooldown_hours": settings.cooldown_hours,
        "entry_enabled": settings.entry_enabled,
        "candidate_scan_interval_seconds": settings.candidate_scan_interval_seconds,
    }
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)
    return payload


def settings_payload(settings: DashboardSettings) -> dict[str, Any]:
    return {
        "strategy_timeframe": settings.strategy_timeframe,
        "max_open_trades": settings.max_open_trades,
        "min_change_20d": settings.min_change_20d,
        "max_change_20d": settings.max_change_20d,
        "max_drawdown_to_gain_ratio_pct": settings.max_drawdown_to_gain_ratio_pct,
        "lookback_days": settings.lookback_days,
        "use_4h_ma_filter": settings.use_4h_ma_filter,
        "use_ma99_filter": settings.use_ma99_filter,
        "ma7_reclaim_enabled": settings.ma7_reclaim_enabled,
        "ma7_reclaim_tolerance_pct": settings.ma7_reclaim_tolerance_pct,
        "ma7_reclaim_lookback_days": settings.ma7_reclaim_lookback_days,
        "ma7_exit_threshold_pct": settings.ma7_exit_threshold_pct,
        "hard_stoploss_pct": settings.hard_stoploss_pct,
        "peak_drawdown_stop_enabled": settings.peak_drawdown_stop_enabled,
        "peak_drawdown_stop_pct": settings.peak_drawdown_stop_pct,
        "dynamic_drawdown_stop_enabled": settings.dynamic_drawdown_stop_enabled,
        "dynamic_drawdown_activation_pct": settings.dynamic_drawdown_activation_pct,
        "dynamic_max_profit_giveback_pct": settings.dynamic_max_profit_giveback_pct,
        "chandelier_exit_enabled": settings.chandelier_exit_enabled,
        "partial_take_profit_enabled": settings.partial_take_profit_enabled,
        "no_progress_exit_enabled": settings.no_progress_exit_enabled,
        "candidate_reentry_required": settings.candidate_reentry_required,
        "cooldown_enabled": settings.cooldown_enabled,
        "cooldown_hours": settings.cooldown_hours,
        "entry_enabled": settings.entry_enabled,
        "candidate_scan_interval_seconds": settings.candidate_scan_interval_seconds,
    }


def write_preview_settings(settings: DashboardSettings) -> None:
    PREVIEW_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    for path in PREVIEW_RESULTS_DIR.iterdir():
        if path.is_file():
            path.unlink()
    temp_path = PREVIEW_SETTINGS_PATH.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(settings_payload(settings), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(PREVIEW_SETTINGS_PATH)


def promote_preview_if_matching(settings: DashboardSettings) -> bool:
    if not preview_active() or not PREVIEW_SETTINGS_PATH.exists():
        return False
    try:
        preview_settings = json.loads(PREVIEW_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    saved_settings = settings_payload(settings)
    if any(
        preview_settings.get(key) != saved_settings.get(key)
        for key in SCREENING_SETTING_KEYS
    ):
        return False

    for source in PREVIEW_RESULTS_DIR.iterdir():
        if source.name == PREVIEW_SETTINGS_PATH.name or not source.is_file():
            continue
        destination = RESULTS_DIR / source.name
        temp_path = destination.with_suffix(f"{destination.suffix}.tmp")
        shutil.copy2(source, temp_path)
        temp_path.replace(destination)
    shutil.rmtree(PREVIEW_RESULTS_DIR)
    return True


def read_universe() -> dict[str, dict[str, Any]]:
    config = read_config()
    scanner_config = config["scanner"]["daily_trend"]
    universe_path = ROOT_DIR / scanner_config["cache_directory"] / "universe.json"
    if not universe_path.exists():
        return {}
    payload = json.loads(universe_path.read_text(encoding="utf-8"))
    return {item["pair"]: item for item in payload.get("pairs", [])}


def daily_cache_path(pair: str) -> Path | None:
    universe = read_universe()
    pair_config = universe.get(pair)
    if not pair_config:
        return None

    config = read_config()
    scanner_config = config["scanner"]["daily_trend"]
    safe_market_id = "".join(
        character if character.isalnum() or character in "_.-" else "_"
        for character in pair_config["market_id"]
    )
    return (
        ROOT_DIR
        / scanner_config["cache_directory"]
        / f"{safe_market_id}-1d.feather"
    )


def read_daily_candles(pair: str, limit: int) -> pd.DataFrame | None:
    path = daily_cache_path(pair)
    if not path or not path.exists():
        return None

    dataframe = pd.read_feather(path)
    dataframe["date"] = pd.to_datetime(dataframe["date"], utc=True)
    return dataframe[OHLCV_COLUMNS].tail(limit).reset_index(drop=True)


def fetch_binance_candles(pair: str, timeframe: str, limit: int) -> pd.DataFrame:
    universe = read_universe()
    pair_config = universe.get(pair)
    if not pair_config:
        raise ValueError(f"Pair {pair} is not in the current universe")

    config = read_config()
    public_url = config["exchange"]["ccxt_config"]["urls"]["api"]["public"]
    query = urlencode(
        {
            "symbol": pair_config["market_id"],
            "interval": timeframe,
            "limit": limit,
        }
    )
    with urlopen(f"{public_url}/klines?{query}", timeout=30) as response:
        rows = json.loads(response.read().decode("utf-8"))

    dataframe = pd.DataFrame(
        [
            [
                row[0],
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
            ]
            for row in rows
        ],
        columns=OHLCV_COLUMNS,
    )
    dataframe["date"] = pd.to_datetime(dataframe["date"], unit="ms", utc=True)
    return dataframe


def beijing_opens_cache_path(pair: str) -> Path:
    universe = read_universe()
    pair_config = universe.get(pair)
    if not pair_config:
        raise ValueError(f"Pair {pair} is not in the current universe")

    config = read_config()
    scanner_config = config["scanner"]["daily_trend"]
    cache_directory = (
        ROOT_DIR / scanner_config["cache_directory"] / "beijing_daily_opens"
    )
    cache_directory.mkdir(parents=True, exist_ok=True)
    safe_market_id = "".join(
        character if character.isalnum() or character in "_.-" else "_"
        for character in pair_config["market_id"]
    )
    return cache_directory / f"{safe_market_id}.json"


def fetch_beijing_daily_opens(
    pair: str,
    start_time: pd.Timestamp,
    end_time: pd.Timestamp,
) -> dict[Any, float]:
    universe = read_universe()
    pair_config = universe.get(pair)
    if not pair_config:
        raise ValueError(f"Pair {pair} is not in the current universe")

    config = read_config()
    public_url = config["exchange"]["ccxt_config"]["urls"]["api"]["public"]
    required_dates = {
        timestamp.tz_convert("Asia/Shanghai").date()
        for timestamp in pd.date_range(
            start_time.normalize(),
            end_time.normalize(),
            freq="1D",
        )
    }
    path = beijing_opens_cache_path(pair)
    cached_payload = {}
    if path.exists():
        try:
            cached_payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached_payload = {}
    daily_opens = {
        pd.Timestamp(date_value).date(): float(opening_price)
        for date_value, opening_price in cached_payload.items()
    }
    missing_dates = required_dates - daily_opens.keys()
    if not missing_dates:
        return daily_opens

    first_missing = min(missing_dates)
    last_missing = max(missing_dates)
    start_utc = pd.Timestamp(first_missing, tz="Asia/Shanghai").tz_convert("UTC")
    end_utc = (
        pd.Timestamp(last_missing, tz="Asia/Shanghai") + pd.Timedelta(days=1)
    ).tz_convert("UTC")
    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(end_utc.timestamp() * 1000)
    rows: list[list[Any]] = []

    while start_ms < end_ms:
        query = urlencode(
            {
                "symbol": pair_config["market_id"],
                "interval": "4h",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 1000,
            }
        )
        with urlopen(f"{public_url}/klines?{query}", timeout=30) as response:
            batch = json.loads(response.read().decode("utf-8"))
        if not batch:
            break
        rows.extend(batch)
        next_start = int(batch[-1][0]) + 4 * 60 * 60 * 1000
        if next_start <= start_ms:
            break
        start_ms = next_start
        if len(batch) < 1000:
            break

    for row in rows:
        timestamp = pd.Timestamp(int(row[0]), unit="ms", tz="UTC")
        beijing_time = timestamp.tz_convert("Asia/Shanghai")
        if beijing_time.hour == 0:
            daily_opens[beijing_time.date()] = float(row[1])
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(
            {
                date_value.isoformat(): opening_price
                for date_value, opening_price in daily_opens.items()
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    temp_path.replace(path)
    return daily_opens


async def ensure_beijing_daily_opens(
    pair: str,
    dataframe: pd.DataFrame,
) -> dict[Any, float]:
    lock = _daily_open_locks.setdefault(pair, asyncio.Lock())
    async with lock:
        return await asyncio.to_thread(
            fetch_beijing_daily_opens,
            pair,
            dataframe["date"].iloc[0],
            dataframe["date"].iloc[-1],
        )


async def prewarm_daily_open_caches() -> None:
    semaphore = asyncio.Semaphore(3)

    async def prewarm_pair(pair: str) -> None:
        async with semaphore:
            try:
                dataframe = read_daily_candles(pair, 365)
                if dataframe is not None and not dataframe.empty:
                    await ensure_beijing_daily_opens(pair, dataframe)
            except Exception:
                return

    await asyncio.gather(
        *(prewarm_pair(candidate["pair"]) for candidate in read_candidates())
    )


def build_chart_payload(
    pair: str,
    timeframe: str,
    limit: int,
    dataframe: pd.DataFrame,
    beijing_daily_opens: dict[Any, float] | None = None,
) -> dict[str, Any]:
    for period in MA_PERIODS:
        dataframe[f"ma_{period}"] = dataframe["close"].rolling(
            window=period,
            min_periods=1 if period == 99 else period,
        ).mean()
    if timeframe == "1d":
        beijing_dates = dataframe["date"].dt.tz_convert("Asia/Shanghai").dt.date
        opening_prices = beijing_dates.map((beijing_daily_opens or {}).get)
        dataframe["change_today"] = (
            dataframe["close"] / pd.Series(opening_prices, index=dataframe.index) - 1
        ) * 100
    else:
        beijing_dates = dataframe["date"].dt.tz_convert("Asia/Shanghai").dt.date
        daily_open = dataframe.groupby(beijing_dates)["open"].transform("first")
        dataframe["change_today"] = (dataframe["close"] / daily_open - 1) * 100
    candles = []
    for row in dataframe.itertuples(index=False):
        candles.append(
            {
                "time": int(row.date.timestamp()),
                "date": row.date.isoformat(),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(row.volume),
                "quoteVolume": float(row.close * row.volume),
                "changeToday": (
                    None if pd.isna(row.change_today) else float(row.change_today)
                ),
                "ma7": None if pd.isna(row.ma_7) else float(row.ma_7),
                "ma20": None if pd.isna(row.ma_20) else float(row.ma_20),
                "ma99": None if pd.isna(row.ma_99) else float(row.ma_99),
            }
        )

    return {
        "pair": pair,
        "timeframe": timeframe,
        "limit": limit,
        "candles": candles,
        "updated_at": pd.Timestamp.now(tz="UTC").isoformat(),
    }


@app.on_event("startup")
async def startup_prewarm() -> None:
    asyncio.create_task(prewarm_daily_open_caches())


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "candidate_count": len(read_candidates())}


@app.get("/api/candidates")
async def candidates() -> dict[str, Any]:
    items = read_candidates()
    return {
        "candidates": items,
        "status": read_status(),
        "is_preview": preview_active(),
        "timeframes": sorted(ALLOWED_TIMEFRAMES, key=("15m", "1h", "4h", "1d").index),
    }


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return read_runtime_settings()


@app.put("/api/settings")
async def update_settings(settings: DashboardSettings) -> dict[str, Any]:
    if _scan_state["running"] or _scan_lock.locked():
        raise HTTPException(status_code=409, detail="扫描进行中，请完成后再保存")
    if settings.max_change_20d <= settings.min_change_20d:
        raise HTTPException(
            status_code=422,
            detail="最大涨幅必须大于最小涨幅",
        )
    saved_settings = write_runtime_settings(settings)
    promoted = promote_preview_if_matching(settings)
    return {
        "status": "saved",
        "settings": saved_settings,
        "preview_promoted": promoted,
    }


async def run_manual_scan(settings: DashboardSettings | None = None) -> None:
    async with _scan_lock:
        is_preview = settings is not None
        if is_preview:
            write_preview_settings(settings)
        _scan_state.update(
            {
                "running": True,
                "is_preview": is_preview,
                "started_at": pd.Timestamp.now(tz="UTC").isoformat(),
                "finished_at": None,
                "return_code": None,
                "message": "正在预览草稿参数..." if is_preview else "正在刷新成交额、日线和 MA...",
            }
        )
        command = [
            sys.executable,
            str(ROOT_DIR / "user_data/scripts/screen_daily_trend.py"),
            "--refresh-universe",
        ]
        if is_preview:
            command.extend(
                [
                    "--settings-file",
                    str(PREVIEW_SETTINGS_PATH),
                    "--output-directory",
                    str(PREVIEW_RESULTS_DIR),
                ]
            )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=ROOT_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output_bytes, _ = await process.communicate()
        output = output_bytes.decode("utf-8", errors="replace").strip()
        final_line = output.splitlines()[-1] if output else ""

        if process.returncode == 0:
            _response_cache.clear()
            global _candidate_pairs_cache
            _candidate_pairs_cache = None
            asyncio.create_task(prewarm_daily_open_caches())
            message = (
                "预览完成，结果尚未用于交易"
                if is_preview
                else final_line or "扫描完成"
            )
        elif process.returncode == 75:
            message = "定时扫描正在运行，请稍后再试"
        else:
            message = final_line or "扫描失败"

        _scan_state.update(
            {
                "running": False,
                "finished_at": pd.Timestamp.now(tz="UTC").isoformat(),
                "return_code": process.returncode,
                "message": message,
            }
        )


@app.post("/api/scan")
async def start_scan(settings: DashboardSettings | None = None) -> dict[str, Any]:
    if _backtest_lock.locked():
        raise HTTPException(status_code=409, detail="回测进行中，请完成后再扫描")
    if _scan_state["running"] or _scan_lock.locked():
        return _scan_state.copy()

    asyncio.create_task(run_manual_scan(settings))
    await asyncio.sleep(0)
    return _scan_state.copy()


@app.get("/api/scan/status")
async def scan_status() -> dict[str, Any]:
    return _scan_state.copy()


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def normalize_4h_backtest_result(
    raw_payload: dict[str, Any],
    days: int,
) -> dict[str, Any]:
    raw_result = raw_payload.get("result")
    if not isinstance(raw_result, dict) or not raw_result.get("summary"):
        raise ValueError("4h 回测未返回有效结果")

    data = raw_payload.get("data") or {}
    summary = raw_result["summary"]
    trades = []
    for trade in raw_result.get("trades", []):
        entry_amount = float(trade.get("entry_amount", trade.get("stake", 0)))
        profit_abs = float(trade.get("profit_abs", 0))
        normalized_trade = dict(trade)
        normalized_trade["entry_amount"] = round(entry_amount, 2)
        normalized_trade["exit_amount"] = round(entry_amount + profit_abs, 2)
        trades.append(normalized_trade)

    generated_at = pd.Timestamp.now(tz="UTC").isoformat()
    return {
        "meta": {
            "generated_at": generated_at,
            "days": days,
            "strategy_timeframe": "4h",
            "scan_timeframe": "4h",
            "execution_timeframe": "4h",
            "universe_size": int(data.get("pairs", 0)),
            "processed_pairs": int(data.get("pairs", 0)),
            "error_count": 0,
            "errors": [],
            "fee_rate": 0.001,
            "settings": raw_result.get("parameters", {}),
            "survivorship_bias_notice": (
                "使用当前可交易池回放，不包含历史期间已退市交易对。"
            ),
        },
        "summary": {
            **summary,
            "position_sizing": "dynamic_compounding",
            "max_open_trades": raw_result.get("parameters", {}).get(
                "max_open_trades",
                2,
            ),
            "stake_formula": "equity_before_entry / max_open_trades",
        },
        "equity_curve": raw_result.get("equity_curve", []),
        "trades": sorted(
            trades,
            key=lambda trade: trade.get("exit_time", ""),
            reverse=True,
        ),
    }


def write_backtest_status(
    *,
    running: bool,
    progress: float,
    message: str,
    **extra: Any,
) -> None:
    payload: dict[str, Any] = {
        "running": running,
        "progress": progress,
        "message": message,
        **extra,
    }
    write_json_file(BACKTEST_STATUS_PATH, payload)


async def run_backtest(request: BacktestRequest) -> None:
    global _backtest_process
    async with _backtest_lock:
        BACKTEST_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        initial_status = {
            "running": True,
            "progress": 0.0,
            "message": (
                f"正在准备 {request.days} 天 4h 回测..."
                if request.strategy_timeframe == "4h" and request.days > 180
                else "正在启动 4h 回测..."
                if request.strategy_timeframe == "4h"
                else "正在启动回测..."
            ),
            "started_at": pd.Timestamp.now(tz="UTC").isoformat(),
        }
        write_json_file(BACKTEST_STATUS_PATH, initial_status)
        is_4h = request.strategy_timeframe == "4h"
        runtime_settings = read_runtime_settings()
        output_path = BACKTEST_4H_RAW_PATH if is_4h else BACKTEST_RESULT_PATH
        command = [
            sys.executable,
            str(
                ROOT_DIR
                / (
                    "user_data/scripts/research_4h_strategy.py"
                    if is_4h
                    else "user_data/scripts/backtest_daily_trend.py"
                )
            ),
            "--days",
            str(request.days),
            "--initial-balance",
            str(request.initial_balance),
            "--output",
            str(output_path),
        ]
        if not is_4h:
            command.extend(
                [
                    "--status-path",
                    str(BACKTEST_STATUS_PATH),
                ]
            )
        else:
            command.extend(
                [
                    "--status-path",
                    str(BACKTEST_STATUS_PATH),
                    "--ma7-exit-threshold-pct",
                    str(runtime_settings["ma7_exit_threshold_pct"]),
                    "--stop-pct",
                    str(runtime_settings["hard_stoploss_pct"]),
                ]
            )
            if runtime_settings["chandelier_exit_enabled"]:
                command.append("--chandelier-exit-enabled")
            if runtime_settings["partial_take_profit_enabled"]:
                command.append("--partial-take-profit-enabled")
            if request.days > 180:
                command.append("--refresh-cache")
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=ROOT_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        _backtest_process = process
        try:
            output_bytes, _ = await process.communicate()
            if process.returncode != 0:
                output = output_bytes.decode("utf-8", errors="replace").strip()
                status = read_json_file(BACKTEST_STATUS_PATH)
                if not status or status.get("running"):
                    message = output.splitlines()[-1] if output else "回测执行失败"
                    failed_status = {
                        "running": False,
                        "progress": 0.0,
                        "message": message,
                        "finished_at": pd.Timestamp.now(tz="UTC").isoformat(),
                    }
                    write_json_file(BACKTEST_STATUS_PATH, failed_status)
            elif is_4h:
                try:
                    normalized_result = normalize_4h_backtest_result(
                        read_json_file(BACKTEST_4H_RAW_PATH),
                        request.days,
                    )
                    write_json_file(BACKTEST_RESULT_PATH, normalized_result)
                    write_backtest_status(
                        running=False,
                        progress=100.0,
                        message="4h 回测完成",
                        finished_at=pd.Timestamp.now(tz="UTC").isoformat(),
                    )
                except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    write_backtest_status(
                        running=False,
                        progress=0.0,
                        message=f"4h 回测结果处理失败：{type(exc).__name__}: {exc}",
                        finished_at=pd.Timestamp.now(tz="UTC").isoformat(),
                    )
        finally:
            _backtest_process = None


@app.post("/api/backtest")
async def start_backtest(request: BacktestRequest) -> dict[str, Any]:
    if _backtest_lock.locked():
        raise HTTPException(status_code=409, detail="已有回测任务正在运行")
    if _scan_state["running"] or _scan_lock.locked():
        raise HTTPException(status_code=409, detail="候选扫描进行中，请稍后再运行回测")
    asyncio.create_task(run_backtest(request))
    await asyncio.sleep(0)
    return read_json_file(BACKTEST_STATUS_PATH)


@app.delete("/api/backtest")
async def cancel_backtest() -> dict[str, Any]:
    process = _backtest_process
    if process is None or process.returncode is not None:
        raise HTTPException(status_code=409, detail="当前没有运行中的回测")
    process.terminate()
    cancelled_status = {
        "running": False,
        "progress": 0.0,
        "message": "回测已停止",
        "finished_at": pd.Timestamp.now(tz="UTC").isoformat(),
    }
    temp_path = BACKTEST_STATUS_PATH.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(cancelled_status, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(BACKTEST_STATUS_PATH)
    return cancelled_status


@app.get("/api/backtest/status")
async def backtest_status() -> dict[str, Any]:
    status = read_json_file(BACKTEST_STATUS_PATH)
    if not status:
        status = {
            "running": False,
            "progress": 0.0,
            "message": "尚未运行回测",
        }
    status["result"] = (
        read_json_file(BACKTEST_RESULT_PATH)
        if BACKTEST_RESULT_PATH.exists()
        else None
    )
    return status


@app.get("/api/candles")
async def candles(
    pair: str = Query(...),
    timeframe: str = Query("1d"),
    limit: int = Query(300, ge=120, le=500),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    if timeframe not in ALLOWED_TIMEFRAMES:
        raise HTTPException(status_code=400, detail="Unsupported timeframe")

    if pair not in candidate_pairs_cached():
        raise HTTPException(status_code=404, detail="Pair is not a current candidate")

    cache_key = (pair, timeframe, limit)
    cached = _response_cache.get(cache_key)
    ttl = _CANDLE_CACHE_TTL.get(timeframe, _CANDLE_CACHE_TTL_DEFAULT)
    if not refresh and cached and time.monotonic() - cached[0] < ttl:
        return cached[1]

    try:
        dataframe = None if refresh else (
            read_daily_candles(pair, limit) if timeframe == "1d" else None
        )
        if dataframe is None:
            dataframe = await asyncio.to_thread(
                fetch_binance_candles,
                pair,
                timeframe,
                limit,
            )
        beijing_daily_opens = None
        if timeframe == "1d" and not dataframe.empty:
            beijing_daily_opens = await ensure_beijing_daily_opens(pair, dataframe)
        payload = build_chart_payload(
            pair,
            timeframe,
            limit,
            dataframe.copy(),
            beijing_daily_opens,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Unable to load candles: {exc}") from exc

    _response_cache[cache_key] = (time.monotonic(), payload)
    return payload
