import csv
import json
from datetime import datetime
from pathlib import Path

from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative, stoploss_from_absolute


class SpotScanStrategy(IStrategy):
    """Enter current trend candidates and exit below daily MA7 with a buffer."""

    INTERFACE_VERSION = 3

    can_short = False
    timeframe = "15m"
    startup_candle_count = 10
    process_only_new_candles = True

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
    _settings_mtime_ns: int | None = None
    _ma7_exit_threshold_pct = 2.0
    _hard_stoploss_pct = 6.0
    _entry_enabled = True

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
        },
    }

    @informative("1d")
    def populate_indicators_1d(
        self,
        dataframe: DataFrame,
        metadata: dict,
    ) -> DataFrame:
        dataframe["ma7"] = dataframe["close"].rolling(7, min_periods=7).mean()
        return dataframe

    def _load_candidate_pairs(self) -> set[str]:
        try:
            mtime_ns = self.candidate_path.stat().st_mtime_ns
        except OSError:
            self._candidate_mtime_ns = None
            self._candidate_pairs = set()
            return self._candidate_pairs

        if mtime_ns == self._candidate_mtime_ns:
            return self._candidate_pairs

        try:
            with self.candidate_path.open(encoding="utf-8", newline="") as csv_file:
                self._candidate_pairs = {
                    row["pair"]
                    for row in csv.DictReader(csv_file)
                    if row.get("pair")
                }
            self._candidate_mtime_ns = mtime_ns
        except (OSError, KeyError):
            self._candidate_pairs = set()
        return self._candidate_pairs

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
            entry_enabled = bool(payload.get("entry_enabled", True))
            self._ma7_exit_threshold_pct = min(
                100.0,
                max(0.0, ma7_threshold),
            )
            self._hard_stoploss_pct = min(
                99.0,
                max(0.1, hard_stoploss),
            )
            self._entry_enabled = entry_enabled
            self._settings_mtime_ns = mtime_ns
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self._ma7_exit_threshold_pct = 2.0
            self._hard_stoploss_pct = 6.0
            self._entry_enabled = True

    def _load_ma7_exit_threshold(self) -> float:
        self._load_trade_settings()
        return self._ma7_exit_threshold_pct

    def _load_hard_stoploss(self) -> float:
        self._load_trade_settings()
        return self._hard_stoploss_pct

    def _load_entry_enabled(self) -> bool:
        self._load_trade_settings()
        return self._entry_enabled

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
        exit_condition = (
            (dataframe["close_1d"] < dataframe["ma7_1d"] * (1 - threshold))
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[exit_condition, ["exit_long", "exit_tag"]] = (
            1,
            "1d_ma7_buffer_break",
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
        return self._load_entry_enabled()

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
        threshold = self._load_hard_stoploss() / 100
        stop_rate = trade.open_rate * (1 - threshold)
        return stoploss_from_absolute(
            stop_rate,
            current_rate,
            is_short=trade.is_short,
            leverage=trade.leverage,
        )
