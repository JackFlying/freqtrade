# Debug Session: live-bot-stopped
- **Status**: [OPEN]
- **Issue**: The live trading bot is stopped unexpectedly.
- **Debug Server**: Pending
- **Log File**: .dbg/trae-debug-log-live-bot-stopped.ndjson

## Reproduction Steps
1. Inspect the live bot state, service history, and effective configuration.
2. Identify the event that transitioned the bot to STOPPED.

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| A | An API or Telegram stop command changed the bot state. | High | Low | Pending |
| B | Service restart applied `initial_state: stopped`. | High | Low | Confirmed |
| C | The process exited or was restarted after an error. | Medium | Low | Rejected |
| D | Exchange or wallet reconciliation triggered a protective stop. | Low | Medium | Rejected |

## Log Evidence
- `2026-08-26 06:26:23 UTC`: systemd was reexecuted by `apt-daily-upgrade.service`.
- `2026-08-26 06:26:25 UTC`: systemd stopped `freqtrade-trade`, `freqtrade-scanner`, and `freqtrade-dashboard`.
- `2026-08-26 06:26:31 UTC`: Freqtrade recorded `SIGINT received, aborting`.
- `2026-08-26 06:26:54 UTC`: systemd restarted `freqtrade-trade`.
- `2026-08-26 06:27:17 UTC`: the new Freqtrade process changed state to `STOPPED`.
- Effective configuration contains `initial_state: "stopped"` and `dry_run: false`.
- The system boot time predates the event, and the service has `NRestarts=0`; this was not a host reboot or a crash-loop.

## Verification Conclusion
The package-upgrade systemd reexecution stopped the Freqtrade services. The restarted
trade process correctly obeyed `initial_state: "stopped"`, so it did not resume live
trading automatically. No exchange, wallet, or strategy error initiated the stop.
