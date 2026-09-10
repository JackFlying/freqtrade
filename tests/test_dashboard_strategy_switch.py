import asyncio
from datetime import datetime, timedelta, timezone

from user_data.dashboard.app import (
    BacktestRequest,
    DashboardSettings,
    Research4hSettings,
    settings_payload,
)
from user_data.scripts.screen_daily_trend import analyze_four_hour_candidate


class FakeExchange:
    def __init__(self, rows: list[list[float]]) -> None:
        self.rows = rows

    async def fetch_ohlcv(self, *_args, **_kwargs) -> list[list[float]]:
        return self.rows


def strategy2_rows() -> list[list[float]]:
    boundary = datetime.now(timezone.utc).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    boundary -= timedelta(hours=boundary.hour % 4)
    start = boundary - timedelta(hours=4 * 120)
    rows = []
    for index in range(120):
        opened_at = start + timedelta(hours=4 * index)
        close = 100.0 + index
        rows.append(
            [
                int(opened_at.timestamp() * 1000),
                close - 0.5,
                close + 0.25,
                close - 1.0,
                close,
                1_000.0,
            ]
        )
    rows.append(
        [
            int(boundary.timestamp() * 1000),
            219.0,
            220.0,
            1.0,
            1.0,
            1_000_000.0,
        ]
    )
    return rows


def test_strategy2_defaults_match_guarded_report() -> None:
    settings = Research4hSettings()

    assert settings.scan_interval_minutes == 240
    assert settings.dynamic_entry_enabled is False
    assert settings.candidate_queue_refill_enabled is True
    assert settings.candidate_queue_exclude_exited is True
    assert settings.volume_factor == 0.4
    assert settings.reward_risk == 3.2
    assert settings.break_even_r == 4.0
    assert settings.max_hold_bars == 18
    assert settings.chandelier_atr_multiplier == 4.0
    assert settings.target_lock_r == 2.0
    assert settings.target_hold_adx_min == 40.0


def test_strategy_timeframe_is_fixed_and_not_an_api_input() -> None:
    assert "strategy_timeframe" not in DashboardSettings.model_fields
    assert "strategy_timeframe" not in BacktestRequest.model_fields

    settings = DashboardSettings(
        active_strategy="strategy1",
        min_change_20d=6,
        max_change_20d=30,
    )

    assert settings_payload(settings)["strategy_timeframe"] == "4h"
    assert settings_payload(settings)["use_4h_ma_filter"] is True


def test_strategy2_scan_ignores_open_four_hour_candle() -> None:
    settings = {
        **Research4hSettings().model_dump(),
        "adx_min": 0.0,
        "rsi_min": 0.0,
        "rsi_max": 100.0,
        "volume_factor": 0.0,
        "atr_pct_min": 0.0,
        "atr_pct_max": 100.0,
    }

    result = asyncio.run(
        analyze_four_hour_candidate(
            FakeExchange(strategy2_rows()),
            "TEST/USDT",
            asyncio.Semaphore(1),
            False,
            False,
            1.0,
            1,
            settings,
        )
    )

    assert result["strategy2_candidate"] is True
    assert result["strategy2_market_alignment"] is True
    assert result["entry_score"] is not None
