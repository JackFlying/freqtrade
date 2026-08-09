from pandas import DataFrame

import talib.abstract as ta

from freqtrade.strategy import IStrategy


class SpotScanStrategy(IStrategy):
    """15 minute Binance spot momentum strategy used by the market scanner."""

    INTERFACE_VERSION = 3

    can_short = False
    timeframe = "15m"
    startup_candle_count = 200
    process_only_new_candles = True

    minimal_roi = {"120": 0.0, "60": 0.01, "0": 0.025}
    stoploss = -0.06
    trailing_stop = False
    use_exit_signal = True

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    plot_config = {
        "main_plot": {
            "ema_20": {"color": "#3b82f6"},
            "ema_50": {"color": "#f59e0b"},
            "ema_200": {"color": "#ef4444"},
        },
        "subplots": {
            "RSI": {"rsi": {"color": "#8b5cf6"}},
            "Volume ratio": {"volume_ratio": {"color": "#10b981"}},
            "Scan score": {"scan_score": {"color": "#06b6d4"}},
        },
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema_50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema_200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["roc_12"] = ta.ROC(dataframe, timeperiod=12)

        dataframe["volume_mean_20"] = dataframe["volume"].rolling(20).mean()
        dataframe["volume_ratio"] = (
            dataframe["volume"] / dataframe["volume_mean_20"].replace(0, float("nan"))
        )
        dataframe["trend_pct"] = (dataframe["close"] / dataframe["ema_50"] - 1) * 100

        trend_score = (dataframe["close"] > dataframe["ema_200"]).astype(float) * 30
        alignment_score = (dataframe["ema_20"] > dataframe["ema_50"]).astype(float) * 25
        rsi_score = ((dataframe["rsi"] - 45) / 20).clip(0, 1) * 20
        volume_score = (dataframe["volume_ratio"] / 2).clip(0, 1) * 15
        momentum_score = ((dataframe["roc_12"] + 2) / 6).clip(0, 1) * 10
        dataframe["scan_score"] = (
            trend_score + alignment_score + rsi_score + volume_score + momentum_score
        ).clip(0, 100)

        dataframe["scan_candidate"] = 0
        candidate = (
            (dataframe["close"] > dataframe["ema_200"])
            & (dataframe["ema_20"] > dataframe["ema_50"])
            & (dataframe["rsi"].between(50, 70))
            & (dataframe["roc_12"] > 0)
            & (dataframe["volume_ratio"] >= 1.2)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[candidate, "scan_candidate"] = 1
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        entry = (
            (dataframe["scan_candidate"] == 1)
            & (dataframe["rsi"] > 55)
            & (dataframe["rsi"].shift(1) <= 55)
        )
        dataframe.loc[entry, ["enter_long", "enter_tag"]] = (1, "trend_rsi_volume")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        exit_condition = (
            (dataframe["close"] < dataframe["ema_20"])
            | (dataframe["rsi"] > 75)
            | (dataframe["ema_20"] < dataframe["ema_50"])
        ) & (dataframe["volume"] > 0)
        dataframe.loc[exit_condition, ["exit_long", "exit_tag"]] = (1, "trend_exit")
        return dataframe
