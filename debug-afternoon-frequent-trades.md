# Debug Session: afternoon-frequent-trades

Status: [OPEN]

## Symptom

用户关闭交易后，服务器下午出现频繁交易，需要确认是实际订单、模拟订单、重复进程还是前端/扫描记录。

## Hypotheses

1. 服务器实际仍有交易进程加载 `dry_run=false`。
2. `freqtrade-trade` 存在重复实例，多个进程同时产生订单。
3. 策略出现重复入场/退出信号或订单超时重试。
4. 所见记录来自扫描器或前端，不是交易数据库中的实际成交。

## Evidence

### Server state

- `freqtrade-trade`, `freqtrade-scanner`, and `freqtrade-dashboard` services are active.
- Only one Freqtrade trade process exists: PID `545195`.
- The Freqtrade bot state is `STOPPED`; the process remains alive to serve the API.
- Effective configuration is `dry_run=false`, `stake_amount=unlimited`, `max_open_trades=1`, and the dedicated live database.

### Live database

The live database contains 9 trades opened between `2026-08-22 05:12:59 UTC` and
`2026-08-22 10:28:15 UTC` (13:12-18:28 China time). Eight of these trades
closed before the next entry; one `EGLD/USDT` trade remains open.

The rapid sequence was:

- `KMNO/USDT`: buy 08:34:00, sell 08:34:04
- `MINA/USDT`: buy 08:34:07, sell 08:34:10
- `STX/USDT`: buy 08:35:04, sell 10:24:17
- `ETC/USDT`: buy 10:24:19, sell 10:24:22
- `BNB/USDT`: buy 10:24:23, sell 10:24:28
- `PENGU/USDT`: buy 10:24:46, sell 10:24:51
- `FLOKI/USDT`: buy 10:25:28, sell 10:25:31
- `EGLD/USDT`: buy 10:28:15, still open

Most exits are marked `chandelier_exit`; `STX/USDT` is marked `stop_loss`.
All entries are tagged `candidate_direct`.

### Effective runtime settings

The server runtime settings file is
`user_data/scan_data/daily_trend/runtime_settings.json` and currently has:

- `entry_enabled=true`
- `chandelier_exit_enabled=true`
- `partial_take_profit_enabled=true`
- `strategy_timeframe=4h`
- `cooldown_enabled=true`, `cooldown_hours=4`

## Analysis

- Hypothesis 1: **confirmed**. The reported trades were real trades because the
  live database contains Binance orders and the effective config had
  `dry_run=false`.
- Hypothesis 2: **rejected**. Process inspection found one trade process only.
- Hypothesis 3: **confirmed, with a configuration/exit interaction**. After each
  chandelier exit, the bot immediately selected another `candidate_direct`
  entry. The chandelier stop can be above the current price when calculated
  from the completed 4h/daily indicator, causing immediate exits. The
  cooldown setting does not prevent a different pair from being selected.
- Hypothesis 4: **rejected**. The records are in `trades_live.sqlite` and
  correspond to filled market orders, not only scanner output.

## Changes

### Fix

- Live strategy filters candidates whose current price is at or below the
  active chandelier stop before score ranking.
- `confirm_trade_entry()` repeats the guard using the proposed order rate.
- Both backtest simulators apply the same guard and continue to the next valid
  ranked candidate instead of consuming the only available slot.

### Verification

- Python syntax checks passed for the live strategy and both simulators.
- Focused assertions passed:
  - entry rate below stop: rejected
  - entry rate equal to stop: rejected
  - entry rate above stop: allowed
  - stopped top-ranked candidate: next valid candidate is selected
- 540-day backtest with the current saved parameters:
  - total return: `1130.67%`
  - maximum drawdown: `35.96%`
  - trades: `386`
  - profit factor: `1.53`

Awaiting production verification before cleanup.

### Deployment

- GitHub commit: `3e367af`
- Production strategy and both simulators were synchronized.
- Server private override now has `initial_state=stopped` so a service restart
  cannot resume live entries automatically.
- Deployment initially produced macOS AppleDouble files (`._*.py`) that caused
  Freqtrade's strategy resolver to fail UTF-8 decoding. Those metadata files
  were removed.
- Post-deployment service state: active, bot state `STOPPED`.
- Post-deployment database check: no new orders; latest order remains ID 21
  from `2026-08-22 10:28:15 UTC`.
- Existing open database trade remains `EGLD/USDT` (trade ID 11).
