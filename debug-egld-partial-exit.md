# Debug Session: egld-partial-exit
- **Status**: [OPEN]
- **Issue**: EGLD is above the configured 15% partial-take-profit threshold but has no partial sell.

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Evidence |
|----|------------|------------|----------|
| A | The bot is STOPPED, so position adjustment is not evaluated. | Medium | Confirmed |
| B | The strategy is RUNNING but receives a profit value below the 15% threshold. | Medium | Rejected |
| C | `adjust_trade_position()` is not being invoked for the open trade. | Medium | Not applicable while stopped |
| D | The partial exit was attempted but Binance rejected or left an order unfilled. | Low | Rejected |

## Evidence
- API `/show_config` reports `state: "stopped"` and `dry_run: false`.
- The EGLD API status reports `profit_ratio: 0.34007424`, no successful exits,
  and no open orders.
- Binance has no EGLD sell fills and no open EGLD orders.
- The process has remained active since `2026-09-02 06:57:07 UTC`, and its only
  state transition was `Changing state to: STOPPED` at `06:58:11 UTC`.

## Conclusion
The service is active but the trading state is STOPPED. Therefore Freqtrade does
not call the position-adjustment callback and no partial take-profit order can be
created. EGLD is currently above the 15% threshold, so starting the trading state
will cause the next adjustment cycle to attempt a real 50% sell.

## Follow-up Exit Evidence
- `2026-09-03 06:18:31 UTC`: 71.04 EGLD was sold at `5.4048229` with
  `partial_take_profit_15pct`.
- `2026-09-03 06:34:32 UTC`: the remaining 71.04 EGLD was sold at `5.214`
  with `partial_trailing_stop`.
- The persisted active stop was `5.225`; the exit rate `5.214` crossed it.
  That stop corresponds to a strategy high-water mark of `5.50` and the
  configured 5% drawdown rule: `5.50 * 0.95 = 5.225`.
