# Debug Session: sql-fida-exits
- **Status**: [OPEN]
- **Issue**: Determine the exact exit cause for SQL and the rapid FIDA exit, and explain the close-reason vocabulary.
- **Debug Server**: Not required; historical database and journal evidence is available.

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Evidence |
|----|------------|------------|----------|
| A | SOL exited because its dynamically tightened Chandelier stop was crossed. | High | Confirmed |
| B | SOL exited through the remaining-position 5% peak drawdown rule. | Low | Rejected |
| C | FIDA was closed by an active Chandelier stop after entry. | High | Confirmed |
| D | FIDA was closed by MA, time, or hard stop. | Low | Rejected |

## Evidence
- No `SQL/USDT` trade exists. The likely intended pair is `SOL/USDT`, trade #14.
- SOL: bought `2026-08-25 07:43:11 UTC` at `101.62`, sold
  `2026-08-26 14:35:37 UTC` at `95.72`, loss `-5.994%`.
  The stored active stop was `95.75`. Reconstructed 4h ATR/Chandelier values show
  a stop of `95.745` before the exit; price crossed it.
- FIDA: bought `2026-08-26 14:39:53 UTC` at `0.01978`, sold
  `2026-08-26 16:00:59 UTC` at `0.0196183`, loss `-1.016%`.
  The 4h Chandelier stop tightened to `0.019661` after the `12:00 UTC` candle;
  the sell price was below that threshold.
- The strategy has `trailing_stop = False`. Freqtrade nevertheless labels a hit
  as `trailing_stop_loss` whenever the current stop differs from the initial
  stop. Here the strategy's `custom_stoploss()` had raised SOL's stop to the
  Chandelier rate.
- FIDA's journal explicitly records `Exit for FIDA/USDT detected. Reason:
  custom_exit`, with the strategy exit reason `chandelier_exit`.

## Conclusion
SOL was stopped out by the dynamically raised Chandelier stop. FIDA was not
immediately sold; it held for 81 minutes and then crossed a Chandelier stop that
had tightened after entry. Neither exit was a manual sell, MA exit, time exit,
or fixed hard-stop exit.
