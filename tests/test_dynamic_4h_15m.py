import numpy as np
import pandas as pd
import talib

from user_data.scripts.dynamic_4h_15m_backtest import (
    DynamicParameters,
    eligible_queue_entries,
    normalize_parameters,
)
from user_data.scripts.dynamic_4h_features import (
    DynamicFeatureCache,
    _directional_states,
    _rsi_states,
)


def test_wilder_rsi_state_matches_talib() -> None:
    rng = np.random.default_rng(20260909)
    close = 100.0 + np.cumsum(rng.normal(size=500))

    _, _, actual = _rsi_states(close, 14)
    expected = talib.RSI(close, timeperiod=14)

    np.testing.assert_allclose(actual, expected, atol=1e-10, rtol=0)


def test_wilder_adx_state_matches_talib() -> None:
    rng = np.random.default_rng(20260909)
    close = 100.0 + np.cumsum(rng.normal(size=500))
    spread = rng.uniform(0.1, 2.0, size=500)
    high = close + spread
    low = close - spread

    *_, actual = _directional_states(high, low, close, 14)
    expected = talib.ADX(high, low, close, timeperiod=14)

    np.testing.assert_allclose(actual, expected, atol=1e-10, rtol=0)


def test_backtest_intervals_do_not_share_a_candle() -> None:
    cache = object.__new__(DynamicFeatureCache)
    cache.timestamps_ns = pd.date_range(
        "2026-01-01T00:15:00Z",
        "2026-01-01T02:00:00Z",
        freq="15min",
    ).as_unit("ns").asi8

    first = cache.interval(
        "2026-01-01T00:00:00Z",
        "2026-01-01T01:00:00Z",
    )
    second = cache.interval(
        "2026-01-01T01:00:00Z",
        "2026-01-01T02:00:00Z",
    )

    assert first == (0, 4)
    assert second == (4, 8)


def test_disabled_features_have_canonical_parameters() -> None:
    parameters = DynamicParameters(
        take_profit_mode="fixed",
        target_trailing_atr=4.0,
        target_lock_r=3.5,
        candidate_replacement_enabled=False,
        replacement_min_score_advantage=24.0,
        replacement_min_hold_minutes=2_880,
    )

    normalized = normalize_parameters(parameters)

    assert normalized.target_trailing_atr == 2.0
    assert normalized.target_lock_r == 0.0
    assert normalized.replacement_min_score_advantage == 5.0
    assert normalized.replacement_min_hold_minutes == 480


def test_candidate_queue_preserves_rank_and_excludes_used_pairs() -> None:
    confirmed_queue = [(7, 100), (3, 100), (9, 100), (4, 100)]

    eligible = eligible_queue_entries(
        confirmed_queue,
        held_pairs={3},
        exited_pairs={7, 4},
    )

    assert eligible == [(9, 100)]
