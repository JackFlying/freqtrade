# Debug Session: stx-observation-entry
- **Status**: [OPEN]
- **Issue**: STX appeared as observation-only in the dashboard but was purchased by
  the live bot.
- **Debug Server**: Pending startup
- **Log File**: `.dbg/trae-debug-log-stx-observation-entry.ndjson`

## Reproduction Steps
1. Inspect the STX live trade and its order timestamps.
2. Compare the scanner candidate snapshot and dashboard observation state at the
   same timestamp.
3. Inspect the live bot's entry decision path.

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| A | Observation-only STX remained in the tradable candidate CSV. | High | Low | Pending |
| B | Scanner/dashboard and trade service read different snapshots or files. | High | Medium | Pending |
| C | STX was bought before its state changed to observation-only. | Medium | Medium | Pending |
| D | Observation state is presentation-only and has no entry constraint. | Medium | Low | Pending |
| E | The entry was manual or forced rather than candidate-driven. | Low | Medium | Pending |

## Log Evidence
- `trae-debug-log-stx-observation-entry.ndjson:1`: the scanner reported STX
  with `trend_candidate=false`, `is_provisional_daily_candle=true`, and
  `candidate_contains_stx=false` on the current scan.
- Live database: STX trade ID 6 opened at `2026-08-22 08:35:04 UTC` and trade
  ID 13 opened at `2026-08-23 10:39:04 UTC`; both have
  `enter_tag=candidate_direct`.
- Live trade log: the first STX order was created at
  `2026-08-22 08:35:04 UTC` with `enter_tag=candidate_direct`.
- Dashboard UI maps `is_provisional_daily_candle` only to the
  `临时日线` badge. The live strategy reads only the candidate CSV and does
  not inspect that field.

## Verification Conclusion
| ID | Hypothesis | Status | Evidence |
|----|------------|--------|----------|
| A | Observation-only STX remained in the tradable candidate CSV. | Confirmed historically | Both historical STX entries are `candidate_direct`; that tag is emitted only for CSV candidates. |
| B | Scanner/dashboard and trade service read different snapshots or files. | Rejected currently | Scanner log shows STX is not in the current candidate CSV, and the current bot reads that same CSV. |
| C | STX was bought before its state changed to observation-only. | Inconclusive | Historical candidate snapshots were not retained. |
| D | Observation state is presentation-only and has no entry constraint. | Confirmed | Dashboard uses the field only for a badge; strategy ignores it. |
| E | The entry was manual or forced rather than candidate-driven. | Rejected | Both database and service log use `candidate_direct`. |
