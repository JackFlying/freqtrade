#!/usr/bin/env python3

import asyncio
import csv
import hmac
import json
import math
import os
import signal
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode
from urllib.request import urlopen

import jwt
import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from freqtrade.configuration.load_config import load_from_files
from pydantic import BaseModel, Field


ROOT_DIR = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "static"
CONFIG_PATH = ROOT_DIR / "user_data/config_scan.json"
RESULTS_DIR = ROOT_DIR / "user_data/scan_results"
CANDIDATES_PATH = RESULTS_DIR / "daily_trend_candidates.csv"
STATUS_PATH = RESULTS_DIR / "daily_trend_status.json"
STRATEGY2_RESULTS_DIR = RESULTS_DIR / "strategy2"
STRATEGY2_CANDIDATES_PATH = STRATEGY2_RESULTS_DIR / "daily_trend_candidates.csv"
STRATEGY2_STATUS_PATH = STRATEGY2_RESULTS_DIR / "daily_trend_status.json"
STRATEGY2_SETTINGS_PATH = ROOT_DIR / "user_data/config_strategy2_parameters.json"
PREVIEW_RESULTS_DIR = RESULTS_DIR / "preview"
PREVIEW_CANDIDATES_PATH = PREVIEW_RESULTS_DIR / "daily_trend_candidates.csv"
PREVIEW_STATUS_PATH = PREVIEW_RESULTS_DIR / "daily_trend_status.json"
PREVIEW_SETTINGS_PATH = PREVIEW_RESULTS_DIR / "settings.json"
BACKTEST_RESULTS_DIR = ROOT_DIR / "user_data/backtest_results"
BACKTEST_RESULT_PATH = BACKTEST_RESULTS_DIR / "latest.json"
BACKTEST_STATUS_PATH = BACKTEST_RESULTS_DIR / "status.json"
BACKTEST_4H_RAW_PATH = BACKTEST_RESULTS_DIR / "research_4h_latest.json"
BACKTEST_DYNAMIC_4H_RAW_PATH = (
    BACKTEST_RESULTS_DIR / "dynamic_4h_15m_dashboard_latest.json"
)
FIXED_4H_540D_SNAPSHOT = (
    ROOT_DIR
    / "user_data/backtest_snapshots/"
    "binance_usdt_spot_20250316T0000_20260907T0000.json"
)
FIXED_4H_540D_START = "2025-03-16T00:00:00Z"
FIXED_4H_540D_END = "2026-09-07T00:00:00Z"
ALLOWED_TIMEFRAMES = {"15m", "1h", "4h", "1d"}
OHLCV_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
MA_PERIODS = (7, 20, 99)
SCREENING_SETTING_KEYS = {
    "active_strategy",
    "min_change_20d",
    "max_change_20d",
    "max_drawdown_to_gain_ratio_pct",
    "lookback_days",
    "use_4h_ma_filter",
    "use_ma99_filter",
    "research_4h",
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


@app.middleware("http")
async def require_authentication(request: Request, call_next):
    path = request.url.path
    public = (
        path == "/"
        or path == "/api/auth/login"
        or path == "/api/health"
        or path.startswith("/static/")
        or request.method == "OPTIONS"
    )
    if public:
        return await call_next(request)

    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return JSONResponse(
            status_code=401,
            content={"detail": "需要登录"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        request.state.user = decode_access_token(token)
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, KeyError, TypeError):
        return JSONResponse(
            status_code=401,
            content={"detail": "登录已失效，请重新登录"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


_response_cache: dict[tuple[str, str, str, int], tuple[float, dict[str, Any]]] = {}
# Daily candles come from the local feather cache and change at most once a day,
# so they can be cached longer. Intraday candles are fetched live from Binance;
# their recent history barely changes, so a modest cache spares repeated network
# round trips when the user toggles timeframes or re-selects a pair.
_CANDLE_CACHE_TTL = {"1d": 300.0}
_CANDLE_CACHE_TTL_DEFAULT = 60.0
# Validating the requested pair against the candidate list would otherwise
# re-parse the CSV on every /api/candles call. Cache it briefly instead.
_candidate_pairs_cache: dict[str, tuple[float, set[str]]] = {}
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


class Research4hSettings(BaseModel):
    scan_interval_minutes: Literal[240] = 240
    dynamic_entry_enabled: bool = False
    candidate_queue_refill_enabled: bool = True
    candidate_queue_exclude_exited: bool = True
    mode: Literal["breakout", "pullback"] = "breakout"
    adx_min: float = Field(default=22.0, ge=0, le=100)
    rsi_min: float = Field(default=38.0, ge=0, le=100)
    rsi_max: float = Field(default=68.0, ge=0, le=100)
    volume_factor: float = Field(default=0.4, ge=0, le=20)
    touch_pct: float = Field(default=2.5, ge=0, le=20)
    breakout_bars: Literal[4, 6, 8, 10, 12, 14, 16, 18, 20, 24, 30] = 10
    market_filter: bool = True
    reward_risk: float = Field(default=3.2, gt=0, le=20)
    break_even_r: float = Field(default=4.0, ge=0, le=20)
    max_hold_bars: int = Field(default=18, ge=1, le=720)
    ema20_slope_min: float = Field(default=-0.1, ge=-10, le=10)
    atr_pct_min: float = Field(default=0.4, ge=0, le=100)
    atr_pct_max: float = Field(default=9.0, gt=0, le=100)
    market_adx_min: float = Field(default=12.0, ge=0, le=100)
    take_profit_mode: Literal[
        "fixed", "adaptive", "trailing", "partial", "none"
    ] = "adaptive"
    target_trailing_atr: float = Field(default=1.0, gt=0, le=20)
    time_exit_mode: Literal[
        "fixed", "trend", "profitable", "runner", "none"
    ] = "runner"
    chandelier_atr_multiplier: float = Field(default=4.0, gt=0, le=20)
    target_partial_fraction: float = Field(default=0.75, gt=0, lt=1)
    target_lock_r: float = Field(default=2.0, ge=0, le=20)
    target_hold_adx_min: float = Field(default=40.0, ge=0, le=100)
    target_hold_slope_min: float = Field(default=0.1, ge=-10, le=10)
    target_hold_rsi_min: float = Field(default=70.0, ge=0, le=100)
    target_hold_volume_ratio_min: float = Field(default=1.0, ge=0, le=20)


class DashboardSettings(BaseModel):
    active_strategy: Literal["strategy1", "strategy2"] = "strategy1"
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
    ma7_exit_threshold_pct: float = Field(default=4.0, ge=0, le=100)
    hard_stoploss_pct: float = Field(default=12.0, ge=0.1, le=99)
    peak_drawdown_stop_enabled: bool = False
    peak_drawdown_stop_pct: float = Field(default=5.0, ge=0.1, le=99)
    dynamic_drawdown_stop_enabled: bool = False
    dynamic_drawdown_activation_pct: float = Field(default=3.0, ge=0.1, le=100)
    dynamic_max_profit_giveback_pct: float = Field(default=5.0, ge=0.5, le=50)
    chandelier_exit_enabled: bool = True
    partial_take_profit_enabled: bool = True
    candidate_replacement_enabled: bool = False
    replacement_min_score_advantage: float = Field(default=0.0, ge=0, le=100)
    replacement_min_hold_bars: int = Field(default=2, ge=1, le=24)
    no_progress_exit_enabled: bool = False
    candidate_reentry_required: bool = False
    cooldown_enabled: bool = False
    cooldown_hours: float = Field(default=4.0, ge=0.1, le=168)
    entry_enabled: bool = True
    candidate_scan_interval_seconds: int = Field(default=900, ge=60, le=86400)
    research_4h: Research4hSettings = Field(
        default_factory=Research4hSettings
    )


class BacktestRequest(BaseModel):
    days: int = Field(default=30, ge=7, le=540)
    initial_balance: float = Field(default=1000.0, ge=100, le=10000000)
    active_strategy: Literal["strategy1", "strategy2"] = "strategy1"


class LoginRequest(BaseModel):
    username: str
    password: str


def read_config() -> dict[str, Any]:
    private_config_path = ROOT_DIR / "user_data/config_private.json"
    return load_from_files([str(CONFIG_PATH), str(private_config_path)])


def auth_config() -> dict[str, str]:
    api_config = read_config().get("api_server", {})
    return {
        "username": str(api_config.get("username", "")),
        "password": str(api_config.get("password", "")),
        "jwt_secret_key": str(api_config.get("jwt_secret_key", "")),
    }


def issue_access_token(username: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + timedelta(hours=8),
    }
    return jwt.encode(payload, auth_config()["jwt_secret_key"], algorithm="HS256")


def decode_access_token(token: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        auth_config()["jwt_secret_key"],
        algorithms=["HS256"],
        options={"require": ["sub", "iat", "exp"]},
    )


def parse_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def preview_active() -> bool:
    return PREVIEW_CANDIDATES_PATH.exists() and PREVIEW_STATUS_PATH.exists()


def read_candidates(
    strategy: Literal["strategy1", "strategy2"] = "strategy1",
    path: Path | None = None,
) -> list[dict[str, Any]]:
    candidate_path = path or (
        STRATEGY2_CANDIDATES_PATH
        if strategy == "strategy2"
        else PREVIEW_CANDIDATES_PATH
        if preview_active()
        else CANDIDATES_PATH
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
                    "adx_4h": parse_number(row.get("adx_4h")),
                    "relative_volume_4h": parse_number(
                        row.get("relative_volume_4h")
                    ),
                    "entry_score": parse_number(row.get("entry_score")),
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


def candidate_pairs_cached(
    strategy: Literal["strategy1", "strategy2"] = "strategy1",
) -> set[str]:
    now = time.monotonic()
    cached = _candidate_pairs_cache.get(strategy)
    if cached and now - cached[0] < _CANDIDATE_PAIRS_TTL:
        return cached[1]
    pairs = {item["pair"] for item in read_candidates(strategy)}
    _candidate_pairs_cache[strategy] = (now, pairs)
    return pairs


def read_status(
    strategy: Literal["strategy1", "strategy2"] = "strategy1",
    path: Path | None = None,
) -> dict[str, Any]:
    status_path = path or (
        STRATEGY2_STATUS_PATH
        if strategy == "strategy2"
        else PREVIEW_STATUS_PATH
        if preview_active()
        else STATUS_PATH
    )
    if not status_path.exists():
        return {}
    return json.loads(status_path.read_text(encoding="utf-8"))


def runtime_settings_path(
    strategy: Literal["strategy1", "strategy2"] = "strategy1",
) -> Path:
    if strategy == "strategy2":
        return STRATEGY2_SETTINGS_PATH
    config = read_config()
    path = (
        ROOT_DIR
        / config["scanner"]["daily_trend"]["cache_directory"]
        / "runtime_settings.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def read_runtime_settings(
    strategy: Literal["strategy1", "strategy2"] = "strategy1",
) -> dict[str, Any]:
    scanner_config = read_config()["scanner"]["daily_trend"]
    default_interval = int(scanner_config["update_interval_seconds"])
    defaults = {
        "active_strategy": strategy,
        "strategy_timeframe": "4h",
        "max_open_trades": 1,
        "min_change_20d": 5.0,
        "max_change_20d": 100.0,
        "max_drawdown_to_gain_ratio_pct": 50.0,
        "lookback_days": 20,
        "use_4h_ma_filter": False,
        "use_ma99_filter": False,
        "ma7_reclaim_enabled": True,
        "ma7_reclaim_tolerance_pct": 2.0,
        "ma7_reclaim_lookback_days": 2,
        "ma7_exit_threshold_pct": 4.0,
        "hard_stoploss_pct": 12.0,
        "peak_drawdown_stop_enabled": False,
        "peak_drawdown_stop_pct": 5.0,
        "dynamic_drawdown_stop_enabled": False,
        "dynamic_drawdown_activation_pct": 3.0,
        "dynamic_max_profit_giveback_pct": 5.0,
        "chandelier_exit_enabled": True,
        "partial_take_profit_enabled": True,
        "candidate_replacement_enabled": False,
        "replacement_min_score_advantage": 0.0,
        "replacement_min_hold_bars": 2,
        "no_progress_exit_enabled": False,
        "candidate_reentry_required": False,
        "cooldown_enabled": False,
        "cooldown_hours": 4.0,
        "entry_enabled": True,
        "candidate_scan_interval_seconds": default_interval,
        "research_4h": Research4hSettings().model_dump(),
    }
    path = runtime_settings_path(strategy)
    if not path.exists():
        return defaults
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        stored_research = payload.get("research_4h")
        normalized_research = Research4hSettings(
            **{
                **defaults["research_4h"],
                **(
                    stored_research
                    if isinstance(stored_research, dict)
                    else {}
                ),
            }
        ).model_dump()
        return {
            "active_strategy": strategy,
            "strategy_timeframe": "4h",
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
            "candidate_replacement_enabled": bool(
                payload.get(
                    "candidate_replacement_enabled",
                    defaults["candidate_replacement_enabled"],
                )
            ),
            "replacement_min_score_advantage": max(
                0.0,
                min(
                    100.0,
                    float(
                        payload.get(
                            "replacement_min_score_advantage",
                            defaults["replacement_min_score_advantage"],
                        )
                    ),
                ),
            ),
            "replacement_min_hold_bars": max(
                1,
                min(
                    24,
                    int(
                        payload.get(
                            "replacement_min_hold_bars",
                            defaults["replacement_min_hold_bars"],
                        )
                    ),
                ),
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
            "research_4h": normalized_research,
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return defaults


def write_runtime_settings(
    settings: DashboardSettings,
) -> dict[str, Any]:
    path = runtime_settings_path(settings.active_strategy)
    payload = {
        "active_strategy": settings.active_strategy,
        "strategy_timeframe": "4h",
        "max_open_trades": settings.max_open_trades,
        "min_change_20d": settings.min_change_20d,
        "max_change_20d": settings.max_change_20d,
        "max_drawdown_to_gain_ratio_pct": settings.max_drawdown_to_gain_ratio_pct,
        "lookback_days": settings.lookback_days,
        "use_4h_ma_filter": True,
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
        "candidate_replacement_enabled": settings.candidate_replacement_enabled,
        "replacement_min_score_advantage": (
            settings.replacement_min_score_advantage
        ),
        "replacement_min_hold_bars": settings.replacement_min_hold_bars,
        "no_progress_exit_enabled": settings.no_progress_exit_enabled,
        "candidate_reentry_required": settings.candidate_reentry_required,
        "cooldown_enabled": settings.cooldown_enabled,
        "cooldown_hours": settings.cooldown_hours,
        "entry_enabled": settings.entry_enabled,
        "candidate_scan_interval_seconds": settings.candidate_scan_interval_seconds,
        "research_4h": settings.research_4h.model_dump(),
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
        "active_strategy": settings.active_strategy,
        "strategy_timeframe": "4h",
        "max_open_trades": settings.max_open_trades,
        "min_change_20d": settings.min_change_20d,
        "max_change_20d": settings.max_change_20d,
        "max_drawdown_to_gain_ratio_pct": settings.max_drawdown_to_gain_ratio_pct,
        "lookback_days": settings.lookback_days,
        "use_4h_ma_filter": True,
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
        "candidate_replacement_enabled": settings.candidate_replacement_enabled,
        "replacement_min_score_advantage": (
            settings.replacement_min_score_advantage
        ),
        "replacement_min_hold_bars": settings.replacement_min_hold_bars,
        "no_progress_exit_enabled": settings.no_progress_exit_enabled,
        "candidate_reentry_required": settings.candidate_reentry_required,
        "cooldown_enabled": settings.cooldown_enabled,
        "cooldown_hours": settings.cooldown_hours,
        "entry_enabled": settings.entry_enabled,
        "candidate_scan_interval_seconds": settings.candidate_scan_interval_seconds,
        "research_4h": settings.research_4h.model_dump(),
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
        *(prewarm_pair(candidate["pair"]) for candidate in read_candidates("strategy1"))
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


@app.post("/api/auth/login")
async def login(request: LoginRequest) -> dict[str, Any]:
    credentials = auth_config()
    if not (
        hmac.compare_digest(request.username, credentials["username"])
        and hmac.compare_digest(request.password, credentials["password"])
    ):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {
        "access_token": issue_access_token(credentials["username"]),
        "token_type": "bearer",
        "expires_in": 8 * 60 * 60,
    }


@app.get("/api/auth/me")
async def current_user(request: Request) -> dict[str, str]:
    return {"username": request.state.user["sub"]}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "candidate_count": len(read_candidates("strategy1")),
        "strategy2_candidate_count": len(read_candidates("strategy2")),
    }


@app.get("/api/candidates")
async def candidates(
    active_strategy: Literal["strategy1", "strategy2"] = Query("strategy1"),
) -> dict[str, Any]:
    items = read_candidates(active_strategy)
    return {
        "candidates": items,
        "status": read_status(active_strategy),
        "is_preview": active_strategy == "strategy1" and preview_active(),
        "active_strategy": active_strategy,
        "timeframes": sorted(ALLOWED_TIMEFRAMES, key=("15m", "1h", "4h", "1d").index),
    }


@app.get("/api/settings")
async def get_settings(
    active_strategy: Literal["strategy1", "strategy2"] = Query("strategy1"),
) -> dict[str, Any]:
    return read_runtime_settings(active_strategy)


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
    promoted = (
        promote_preview_if_matching(settings)
        if settings.active_strategy == "strategy1"
        else False
    )
    return {
        "status": "saved",
        "settings": saved_settings,
        "preview_promoted": promoted,
    }


async def run_manual_scan(
    active_strategy: Literal["strategy1", "strategy2"] = "strategy1",
    settings: DashboardSettings | None = None,
) -> None:
    async with _scan_lock:
        is_strategy2 = active_strategy == "strategy2"
        is_preview = settings is not None and not is_strategy2
        if is_preview:
            write_preview_settings(settings)
        strategy_label = "策略2" if is_strategy2 else "策略1"
        _scan_state.update(
            {
                "running": True,
                "active_strategy": active_strategy,
                "is_preview": is_preview,
                "started_at": pd.Timestamp.now(tz="UTC").isoformat(),
                "finished_at": None,
                "return_code": None,
                "message": (
                    "正在预览草稿参数..."
                    if is_preview
                    else f"正在扫描{strategy_label}候选..."
                ),
            }
        )
        command = [
            sys.executable,
            str(ROOT_DIR / "user_data/scripts/screen_daily_trend.py"),
            "--refresh-universe",
        ]
        if is_strategy2:
            command.extend(
                [
                    "--config",
                    str(ROOT_DIR / "user_data/config_strategy2_dry.json"),
                    "--settings-file",
                    str(STRATEGY2_SETTINGS_PATH),
                ]
            )
        elif is_preview:
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
            start_new_session=True,
        )
        output_bytes, _ = await process.communicate()
        output = output_bytes.decode("utf-8", errors="replace").strip()
        final_line = output.splitlines()[-1] if output else ""

        if process.returncode == 0:
            _response_cache.clear()
            _candidate_pairs_cache.clear()
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
async def start_scan(
    active_strategy: Literal["strategy1", "strategy2"] = Query("strategy1"),
    settings: DashboardSettings | None = None,
) -> dict[str, Any]:
    if _backtest_lock.locked():
        raise HTTPException(status_code=409, detail="回测进行中，请完成后再扫描")
    if _scan_state["running"] or _scan_lock.locked():
        return _scan_state.copy()
    if settings is not None and settings.active_strategy != active_strategy:
        raise HTTPException(status_code=422, detail="策略选择与参数不一致")

    asyncio.create_task(run_manual_scan(active_strategy, settings))
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
    active_strategy: Literal["strategy1", "strategy2"] = "strategy1",
) -> dict[str, Any]:
    is_strategy2 = active_strategy == "strategy2"
    raw_result = raw_payload if is_strategy2 else raw_payload.get("result")
    if not isinstance(raw_result, dict) or not raw_result.get("summary"):
        raise ValueError("4h 回测未返回有效结果")

    data = raw_payload.get("data") or {}
    if is_strategy2:
        data = {
            "pairs": 0,
            "universe_snapshot": str(FIXED_4H_540D_SNAPSHOT),
        }
    snapshot_metadata: dict[str, Any] = {}
    snapshot_value = data.get("universe_snapshot")
    if snapshot_value:
        snapshot_path = Path(str(snapshot_value))
        if snapshot_path.exists():
            snapshot_payload = read_json_file(snapshot_path)
            snapshot_metadata = {
                "path": str(snapshot_path),
                "pair_count": int(snapshot_payload.get("pair_count", 0)),
                "pair_set_sha256": snapshot_payload.get("pair_set_sha256"),
                "data_set_sha256": snapshot_payload.get("data_set_sha256"),
                "start": snapshot_payload.get("start"),
                "end": snapshot_payload.get("end"),
            }
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
            "active_strategy": active_strategy,
            "strategy_timeframe": "4h",
            "scan_timeframe": "4h",
            "execution_timeframe": "15m" if is_strategy2 else "4h",
            "universe_size": int(
                snapshot_metadata.get("pair_count", data.get("pairs", 0))
            ),
            "processed_pairs": int(
                snapshot_metadata.get("pair_count", data.get("pairs", 0))
            ),
            "error_count": 0,
            "errors": [],
            "fee_rate": 0.001,
            "settings": raw_result.get("parameters", {}),
            "universe_snapshot": snapshot_metadata,
            "survivorship_bias_notice": (
                "使用 Binance 历史 USDT 现货主表，包含区间内已下架交易对；"
                "历史 Monitoring/Seed 标签无官方时序数据，未做追溯过滤。"
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
        is_strategy2 = request.active_strategy == "strategy2"
        is_4h = True
        strategy_label = "策略2" if is_strategy2 else "策略1"
        initial_status = {
            "running": True,
            "progress": 0.0,
            "message": (
                f"正在准备 {request.days} 天{strategy_label}回测..."
                if is_4h and request.days > 180
                else f"正在启动{strategy_label}回测..."
            ),
            "started_at": pd.Timestamp.now(tz="UTC").isoformat(),
        }
        write_json_file(BACKTEST_STATUS_PATH, initial_status)
        runtime_settings = read_runtime_settings(request.active_strategy)
        output_path = (
            BACKTEST_DYNAMIC_4H_RAW_PATH
            if is_strategy2
            else BACKTEST_4H_RAW_PATH
            if is_4h
            else BACKTEST_RESULT_PATH
        )
        if is_strategy2:
            fixed_end = pd.Timestamp(FIXED_4H_540D_END)
            fixed_start = max(
                pd.Timestamp(FIXED_4H_540D_START),
                fixed_end - pd.Timedelta(days=request.days),
            )
            command = [
                sys.executable,
                str(
                    ROOT_DIR
                    / "user_data/scripts/dynamic_4h_15m_backtest.py"
                ),
                "--snapshot",
                str(FIXED_4H_540D_SNAPSHOT),
                "--parameters",
                str(runtime_settings_path(request.active_strategy)),
                "--start",
                fixed_start.isoformat(),
                "--end",
                fixed_end.isoformat(),
                "--initial-balance",
                str(request.initial_balance),
                "--output",
                str(output_path),
            ]
        else:
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
        elif not is_strategy2:
            command.extend(
                [
                    "--status-path",
                    str(BACKTEST_STATUS_PATH),
                    "--parameters-path",
                    str(runtime_settings_path(request.active_strategy)),
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
            if runtime_settings["candidate_replacement_enabled"]:
                command.extend(
                    [
                        "--candidate-replacement-enabled",
                        "--replacement-min-score-advantage",
                        str(runtime_settings["replacement_min_score_advantage"]),
                        "--replacement-min-hold-bars",
                        str(runtime_settings["replacement_min_hold_bars"]),
                    ]
                )
            if request.days == 540 and FIXED_4H_540D_SNAPSHOT.exists():
                command.extend(
                    [
                        "--start",
                        FIXED_4H_540D_START,
                        "--end",
                        FIXED_4H_540D_END,
                        "--universe-snapshot",
                        str(FIXED_4H_540D_SNAPSHOT),
                    ]
                )
            else:
                command.append("--historical-market")
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=ROOT_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
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
                        read_json_file(output_path),
                        request.days,
                        request.active_strategy,
                    )
                    write_json_file(BACKTEST_RESULT_PATH, normalized_result)
                    write_backtest_status(
                        running=False,
                        progress=100.0,
                        message=f"{strategy_label}回测完成",
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
    if request.active_strategy == "strategy2" and not FIXED_4H_540D_SNAPSHOT.exists():
        raise HTTPException(
            status_code=409,
            detail="策略2历史回测数据尚未部署到服务器，实时模拟不受影响",
        )
    if request.active_strategy != read_runtime_settings(
        request.active_strategy
    )["active_strategy"]:
        raise HTTPException(status_code=409, detail="请先保存当前策略再运行回测")
    asyncio.create_task(run_backtest(request))
    await asyncio.sleep(0)
    return read_json_file(BACKTEST_STATUS_PATH)


@app.delete("/api/backtest")
async def cancel_backtest() -> dict[str, Any]:
    process = _backtest_process
    if process is None or process.returncode is not None:
        raise HTTPException(status_code=409, detail="当前没有运行中的回测")
    os.killpg(process.pid, signal.SIGTERM)
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
    active_strategy: Literal["strategy1", "strategy2"] = Query("strategy1"),
) -> dict[str, Any]:
    if timeframe not in ALLOWED_TIMEFRAMES:
        raise HTTPException(status_code=400, detail="Unsupported timeframe")

    if pair not in candidate_pairs_cached(active_strategy):
        raise HTTPException(status_code=404, detail="Pair is not a current candidate")

    cache_key = (active_strategy, pair, timeframe, limit)
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
