import csv
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from pandas import DataFrame
from talib import abstract as ta

from freqtrade.exchange import timeframe_to_prev_date
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative, stoploss_from_absolute


class SpotScanStrategy(IStrategy):
    """Enter trend candidates and manage exits on the selected strategy period."""

    INTERFACE_VERSION = 3

    can_short = False
    timeframe = "15m"
    startup_candle_count = 30
    process_only_new_candles = True
    position_adjustment_enable = True

    minimal_roi = {}
    stoploss = -0.99
    use_custom_stoploss = True
    trailing_stop = False
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    candidate_path = (
        Path(__file__).resolve().parents[2]
        / "user_data/scan_results/daily_trend_candidates.csv"
    )
    runtime_settings_path = (
        Path(__file__).resolve().parents[2]
        / "user_data/scan_data/daily_trend/runtime_settings.json"
    )
    _candidate_mtime_ns: int | None = None
    _candidate_pairs: set[str] = set()
    _candidate_quote_volumes: dict[str, tuple[float, int]] = {}
    _settings_mtime_ns: int | None = None
    _ma7_exit_threshold_pct = 2.0
    _hard_stoploss_pct = 6.0
    _peak_drawdown_stop_enabled = False
    _peak_drawdown_stop_pct = 5.0
    _dynamic_drawdown_stop_enabled = False
    _dynamic_drawdown_activation_pct = 3.0
    _dynamic_max_profit_giveback_pct = 5.0
    _chandelier_exit_enabled = False
    _partial_take_profit_enabled = False
    _no_progress_exit_enabled = False
    _candidate_reentry_required = False
    _cooldown_enabled = False
    _cooldown_hours = 4.0
    _entry_enabled = True
    _strategy_timeframe = "1d"
    chandelier_atr_period = 22
    chandelier_atr_multiplier = 3.0
    partial_take_profit_trigger = 0.15
    partial_take_profit_fraction = 0.5
    partial_trailing_drawdown = 0.05

    order_types = {
        "entry": "market",
        "exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    plot_config = {
        "main_plot": {
            "ma7_1d": {"color": "#0788b5"},
            "ma7_4h": {"color": "#0788b5"},
        },
    }

    @informative("1d")
    def populate_indicators_1d(
        self,
        dataframe: DataFrame,
        metadata: dict,
    ) -> DataFrame:
        return self._add_period_exit_indicators(dataframe)

    @informative("4h")
    def populate_indicators_4h(
        self,
        dataframe: DataFrame,
        metadata: dict,
    ) -> DataFrame:
        return self._add_period_exit_indicators(dataframe)

    def _add_period_exit_indicators(self, dataframe: DataFrame) -> DataFrame:
        dataframe["ma7"] = dataframe["close"].rolling(7, min_periods=7).mean()
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["volume_sma20"] = dataframe["volume"].rolling(
            20,
            min_periods=20,
        ).mean()
        previous_close = dataframe["close"].shift(1)
        true_range = pd.concat(
            [
                dataframe["high"] - dataframe["low"],
                (dataframe["high"] - previous_close).abs(),
                (dataframe["low"] - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        dataframe["chandelier_atr"] = true_range.rolling(
            self.chandelier_atr_period,
            min_periods=self.chandelier_atr_period,
        ).mean()
        dataframe["chandelier_high"] = dataframe["high"].rolling(
            self.chandelier_atr_period,
            min_periods=self.chandelier_atr_period,
        ).max()
        return dataframe

    def _load_candidate_pairs(self) -> set[str]:
        try:
            mtime_ns = self.candidate_path.stat().st_mtime_ns
        except OSError:
            self._candidate_mtime_ns = None
            self._candidate_pairs = set()
            self._candidate_quote_volumes = {}
            return self._candidate_pairs

        if mtime_ns == self._candidate_mtime_ns:
            return self._candidate_pairs

        try:
            with self.candidate_path.open(encoding="utf-8", newline="") as csv_file:
                rows = list(csv.DictReader(csv_file))
                self._candidate_pairs = {
                    row["pair"] for row in rows if row.get("pair")
                }
                self._candidate_quote_volumes = {}
                for index, row in enumerate(rows):
                    pair = row.get("pair")
                    if not pair:
                        continue
                    try:
                        volume = float(row.get("quote_volume_24h") or 0)
                    except (TypeError, ValueError):
                        volume = 0.0
                    try:
                        rank = int(row.get("rank") or index + 1)
                    except (TypeError, ValueError):
                        rank = index + 1
                    self._candidate_quote_volumes[pair] = (volume, rank)
            self._candidate_mtime_ns = mtime_ns
            self._refresh_candidate_reentry_states()
        except (OSError, KeyError, TypeError, ValueError):
            self._candidate_pairs = set()
            self._candidate_quote_volumes = {}
        return self._candidate_pairs

    def _entry_score(self, pair: str) -> float:
        if not self.dp:
            return float("-inf")
        try:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe.empty:
                return float("-inf")
            row = dataframe.iloc[-1]
            adx = row.get("adx_4h", row.get("adx"))
            volume = row.get("volume_4h", row.get("volume"))
            volume_sma20 = row.get(
                "volume_sma20_4h",
                row.get("volume_sma20"),
            )
            if pd.isna(adx) or pd.isna(volume) or pd.isna(volume_sma20):
                return float("-inf")
            if float(volume_sma20) <= 0:
                return float("-inf")
            return float(adx) + float(volume) / float(volume_sma20)
        except (AttributeError, KeyError, TypeError, ValueError):
            return float("-inf")

    def _is_highest_score_entry(
        self,
        pair: str,
        current_time: datetime,
    ) -> bool:
        """Allow the highest ADX plus relative-volume candidate to enter first."""
        candidates = self._load_candidate_pairs()
        if pair not in candidates:
            return False

        eligible = [
            candidate
            for candidate in candidates
            if not self._is_pair_in_cooldown(candidate, current_time)
            and not self._is_pair_waiting_for_reentry(candidate)
        ]
        eligible.sort(
            key=lambda candidate: (
                -self._entry_score(candidate),
                -self._candidate_quote_volumes.get(candidate, (0.0, 0))[0],
                self._candidate_quote_volumes.get(candidate, (0.0, 0))[1],
                candidate,
            )
        )
        max_open_trades = int(self.config.get("max_open_trades", 1))
        try:
            open_trade_count = len(Trade.get_trades_proxy(is_open=True))
        except (AttributeError, TypeError):
            open_trade_count = 0
        slots = max_open_trades - open_trade_count
        if slots <= 0:
            return False
        return pair in set(eligible[:slots])

    def _refresh_candidate_reentry_states(self) -> None:
        if not self._candidate_reentry_required:
            return
        try:
            closed_trades = Trade.get_trades_proxy(is_open=False)
        except (AttributeError, TypeError):
            return
        latest_by_pair: dict[str, Trade] = {}
        for trade in closed_trades:
            previous = latest_by_pair.get(trade.pair)
            if previous is None or (
                trade.close_date_utc
                and (
                    previous.close_date_utc is None
                    or trade.close_date_utc > previous.close_date_utc
                )
            ):
                latest_by_pair[trade.pair] = trade
        for pair, trade in latest_by_pair.items():
            if (
                pair not in self._candidate_pairs
                and trade.get_custom_data("candidate_reentry_state")
                == "waiting_for_absence"
            ):
                trade.set_custom_data("candidate_reentry_state", "ready")

    def _load_trade_settings(self) -> None:
        try:
            mtime_ns = self.runtime_settings_path.stat().st_mtime_ns
        except OSError:
            return

        if mtime_ns == self._settings_mtime_ns:
            return

        try:
            payload = json.loads(
                self.runtime_settings_path.read_text(encoding="utf-8")
            )
            ma7_threshold = float(payload.get("ma7_exit_threshold_pct", 2.0))
            hard_stoploss = float(payload.get("hard_stoploss_pct", 6.0))
            peak_drawdown_stop_enabled = bool(
                payload.get("peak_drawdown_stop_enabled", False)
            )
            peak_drawdown_stop = float(
                payload.get("peak_drawdown_stop_pct", 5.0)
            )
            dynamic_drawdown_stop_enabled = bool(
                payload.get("dynamic_drawdown_stop_enabled", False)
            )
            dynamic_drawdown_activation = float(
                payload.get("dynamic_drawdown_activation_pct", 3.0)
            )
            dynamic_max_profit_giveback = float(
                payload.get("dynamic_max_profit_giveback_pct", 5.0)
            )
            chandelier_exit_enabled = bool(
                payload.get("chandelier_exit_enabled", False)
            )
            partial_take_profit_enabled = bool(
                payload.get("partial_take_profit_enabled", False)
            )
            no_progress_exit_enabled = bool(
                payload.get("no_progress_exit_enabled", False)
            )
            candidate_reentry_required = bool(
                payload.get("candidate_reentry_required", False)
            )
            cooldown_enabled = bool(payload.get("cooldown_enabled", False))
            cooldown_hours = float(
                payload.get(
                    "cooldown_hours",
                    float(payload.get("cooldown_minutes", 240)) / 60,
                )
            )
            entry_enabled = bool(payload.get("entry_enabled", True))
            strategy_timeframe = payload.get("strategy_timeframe", "1d")
            self._ma7_exit_threshold_pct = min(
                100.0,
                max(0.0, ma7_threshold),
            )
            self._hard_stoploss_pct = min(
                99.0,
                max(0.1, hard_stoploss),
            )
            self._peak_drawdown_stop_enabled = peak_drawdown_stop_enabled
            self._peak_drawdown_stop_pct = min(
                99.0,
                max(0.1, peak_drawdown_stop),
            )
            self._dynamic_drawdown_stop_enabled = dynamic_drawdown_stop_enabled
            self._dynamic_drawdown_activation_pct = min(
                100.0,
                max(0.1, dynamic_drawdown_activation),
            )
            self._dynamic_max_profit_giveback_pct = min(
                50.0,
                max(0.5, dynamic_max_profit_giveback),
            )
            self._chandelier_exit_enabled = chandelier_exit_enabled
            self._partial_take_profit_enabled = partial_take_profit_enabled
            self._no_progress_exit_enabled = no_progress_exit_enabled
            self._candidate_reentry_required = candidate_reentry_required
            self._cooldown_enabled = cooldown_enabled
            self._cooldown_hours = min(168.0, max(0.1, cooldown_hours))
            self._entry_enabled = entry_enabled
            self._strategy_timeframe = (
                strategy_timeframe if strategy_timeframe in {"1d", "4h"} else "1d"
            )
            self._settings_mtime_ns = mtime_ns
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self._ma7_exit_threshold_pct = 2.0
            self._hard_stoploss_pct = 6.0
            self._peak_drawdown_stop_enabled = False
            self._peak_drawdown_stop_pct = 5.0
            self._dynamic_drawdown_stop_enabled = False
            self._dynamic_drawdown_activation_pct = 3.0
            self._dynamic_max_profit_giveback_pct = 5.0
            self._chandelier_exit_enabled = False
            self._partial_take_profit_enabled = False
            self._no_progress_exit_enabled = False
            self._candidate_reentry_required = False
            self._cooldown_enabled = False
            self._cooldown_hours = 4.0
            self._entry_enabled = True
            self._strategy_timeframe = "1d"

    def _load_ma7_exit_threshold(self) -> float:
        self._load_trade_settings()
        return self._ma7_exit_threshold_pct

    def _load_hard_stoploss(self) -> float:
        self._load_trade_settings()
        return self._hard_stoploss_pct

    def _load_peak_drawdown_stop(self) -> tuple[bool, float]:
        self._load_trade_settings()
        return (
            self._peak_drawdown_stop_enabled,
            self._peak_drawdown_stop_pct,
        )

    def _load_entry_enabled(self) -> bool:
        self._load_trade_settings()
        return self._entry_enabled

    def _highest_candle_rate(
        self,
        pair: str,
        trade: Trade,
        current_rate: float,
    ) -> float:
        peak_rate = max(
            trade.open_rate,
            current_rate,
            float(getattr(trade, "max_rate", trade.open_rate) or trade.open_rate),
        )
        if not self.dp:
            return peak_rate
        try:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe.empty or not {"date", "high"}.issubset(dataframe.columns):
                return peak_rate
            open_candle = timeframe_to_prev_date(
                self.timeframe,
                trade.open_date_utc,
            )
            highs = dataframe.loc[dataframe["date"] >= open_candle, "high"].dropna()
            if not highs.empty:
                peak_rate = max(peak_rate, float(highs.max()))
        except (AttributeError, KeyError, TypeError, ValueError):
            return peak_rate
        return peak_rate

    def _dynamic_stop_rate(
        self,
        pair: str,
        trade: Trade,
        current_rate: float,
    ) -> float | None:
        self._load_trade_settings()
        if not self._dynamic_drawdown_stop_enabled:
            return None
        peak_rate = self._highest_candle_rate(pair, trade, current_rate)
        peak_profit_pct = max(
            0.0,
            (peak_rate / trade.open_rate - 1) * 100,
        )
        if peak_profit_pct < self._dynamic_drawdown_activation_pct:
            return None
        allowed_giveback_pct = min(
            peak_profit_pct * 0.5,
            self._dynamic_max_profit_giveback_pct,
        )
        locked_profit_pct = peak_profit_pct - allowed_giveback_pct
        return trade.open_rate * (1 + locked_profit_pct / 100)

    def _chandelier_stop_rate(self, pair: str) -> float | None:
        self._load_trade_settings()
        if not self._chandelier_exit_enabled or not self.dp:
            return None
        suffix = self._strategy_timeframe
        high_column = f"chandelier_high_{suffix}"
        atr_column = f"chandelier_atr_{suffix}"
        try:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe.empty or not {high_column, atr_column}.issubset(
                dataframe.columns
            ):
                return None
            latest = dataframe.iloc[-1]
            high = float(latest[high_column])
            atr = float(latest[atr_column])
            if not math.isfinite(high) or not math.isfinite(atr):
                return None
            return high - atr * self.chandelier_atr_multiplier
        except (AttributeError, KeyError, TypeError, ValueError):
            return None

    def _partial_trailing_stop_rate(
        self,
        pair: str,
        trade: Trade,
        current_rate: float,
    ) -> float | None:
        self._load_trade_settings()
        if (
            not self._partial_take_profit_enabled
            or trade.nr_of_successful_exits < 1
        ):
            return None
        peak_rate = self._highest_candle_rate(pair, trade, current_rate)
        return peak_rate * (1 - self.partial_trailing_drawdown)

    def _is_pair_in_cooldown(self, pair: str, current_time: datetime) -> bool:
        self._load_trade_settings()
        if not self._cooldown_enabled:
            return False
        try:
            closed_trades = Trade.get_trades_proxy(pair=pair, is_open=False)
        except (AttributeError, TypeError):
            return False
        if not closed_trades:
            return False
        last_trade = max(
            closed_trades,
            key=lambda trade: trade.close_date_utc or datetime.min,
        )
        if not last_trade.close_date_utc:
            return False
        elapsed = current_time - last_trade.close_date_utc
        return elapsed.total_seconds() < self._cooldown_hours * 3600

    def _is_pair_waiting_for_reentry(self, pair: str) -> bool:
        self._load_trade_settings()
        if not self._candidate_reentry_required:
            return False
        try:
            closed_trades = Trade.get_trades_proxy(pair=pair, is_open=False)
        except (AttributeError, TypeError):
            return False
        if not closed_trades:
            return False
        last_trade = max(
            closed_trades,
            key=lambda trade: trade.close_date_utc or datetime.min,
        )
        state = last_trade.get_custom_data("candidate_reentry_state")
        if state not in {"waiting_for_absence", "ready"}:
            return False
        candidate_pairs = self._load_candidate_pairs()
        if state == "waiting_for_absence":
            if pair not in candidate_pairs:
                last_trade.set_custom_data("candidate_reentry_state", "ready")
            return True
        return pair not in candidate_pairs

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["is_daily_candidate"] = int(
            metadata["pair"] in self._load_candidate_pairs()
        )
        dataframe["entry_enabled"] = int(self._load_entry_enabled())
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        entry = (
            (dataframe["entry_enabled"] == 1)
            & (dataframe["is_daily_candidate"] == 1)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[entry, ["enter_long", "enter_tag"]] = (1, "candidate_direct")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        threshold = self._load_ma7_exit_threshold() / 100
        self._load_trade_settings()
        close_column = (
            "close_4h" if self._strategy_timeframe == "4h" else "close_1d"
        )
        ma7_column = "ma7_4h" if self._strategy_timeframe == "4h" else "ma7_1d"
        exit_condition = (
            (dataframe[close_column] < dataframe[ma7_column] * (1 - threshold))
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[exit_condition, ["exit_long", "exit_tag"]] = (
            1,
            f"{self._strategy_timeframe}_ma7_buffer_break",
        )
        return dataframe

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> bool:
        return (
            self._load_entry_enabled()
            and self._is_highest_score_entry(pair, current_time)
        )

    def adjust_trade_position(
        self,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        min_stake: float | None,
        max_stake: float,
        current_entry_rate: float,
        current_exit_rate: float,
        current_entry_profit: float,
        current_exit_profit: float,
        **kwargs,
    ) -> float | tuple[float | None, str | None] | None:
        self._load_trade_settings()
        if (
            not self._partial_take_profit_enabled
            or trade.nr_of_successful_exits > 0
            or trade.has_open_orders
            or current_exit_profit < self.partial_take_profit_trigger
        ):
            return None
        reduction = trade.stake_amount * self.partial_take_profit_fraction
        if min_stake is not None and reduction < min_stake:
            return None
        return -reduction, "partial_take_profit_15pct"

    def confirm_trade_exit(
        self,
        pair: str,
        trade: Trade,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        exit_reason: str,
        current_time: datetime,
        **kwargs,
    ) -> bool:
        self._load_trade_settings()
        if (
            self._candidate_reentry_required
            and exit_reason != "partial_take_profit_15pct"
        ):
            candidate_pairs = self._load_candidate_pairs()
            trade.set_custom_data(
                "candidate_reentry_state",
                (
                    "waiting_for_absence"
                    if pair in candidate_pairs
                    else "ready"
                ),
            )
        return True

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> str | bool:
        dynamic_stop_rate = self._dynamic_stop_rate(pair, trade, current_rate)
        hard_stop_rate = trade.open_rate * (
            1 - self._load_hard_stoploss() / 100
        )
        stop_candidates = [
            (dynamic_stop_rate, "dynamic_peak_drawdown"),
            (self._chandelier_stop_rate(pair), "chandelier_exit"),
            (
                self._partial_trailing_stop_rate(pair, trade, current_rate),
                "partial_trailing_stop",
            ),
        ]
        active_stops = [
            (rate, reason)
            for rate, reason in stop_candidates
            if rate is not None and rate > hard_stop_rate
        ]
        if active_stops:
            stop_rate, reason = max(active_stops, key=lambda item: item[0])
            if current_rate <= stop_rate:
                return reason
        if (
            self._no_progress_exit_enabled
            and current_time - trade.open_date_utc >= timedelta(hours=12)
        ):
            peak_rate = self._highest_candle_rate(pair, trade, current_rate)
            peak_profit_pct = (peak_rate / trade.open_rate - 1) * 100
            if peak_profit_pct < 2.0:
                return "no_progress_12h"
        return False

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> float:
        hard_stop_rate = trade.open_rate * (
            1 - self._load_hard_stoploss() / 100
        )
        stop_rate = hard_stop_rate
        peak_stop_enabled, peak_drawdown_pct = self._load_peak_drawdown_stop()
        if peak_stop_enabled:
            peak_rate = max(
                trade.open_rate,
                float(getattr(trade, "max_rate", trade.open_rate) or trade.open_rate),
            )
            peak_stop_rate = peak_rate * (1 - peak_drawdown_pct / 100)
            # The peak-based stop can only tighten the existing hard stop.
            stop_rate = max(hard_stop_rate, peak_stop_rate)
        if self._dynamic_drawdown_stop_enabled:
            dynamic_stop_rate = self._dynamic_stop_rate(
                pair,
                trade,
                current_rate,
            )
            if dynamic_stop_rate is not None:
                stop_rate = max(stop_rate, dynamic_stop_rate)
        chandelier_stop_rate = self._chandelier_stop_rate(pair)
        if chandelier_stop_rate is not None:
            stop_rate = max(stop_rate, chandelier_stop_rate)
        partial_trailing_stop_rate = self._partial_trailing_stop_rate(
            pair,
            trade,
            current_rate,
        )
        if partial_trailing_stop_rate is not None:
            stop_rate = max(stop_rate, partial_trailing_stop_rate)
        return stoploss_from_absolute(
            stop_rate,
            current_rate,
            is_short=trade.is_short,
            leverage=trade.leverage,
        )
