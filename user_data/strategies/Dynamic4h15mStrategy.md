# Strategy 2 Dry-Run

`Dynamic4h15mStrategy` is an isolated dry-run implementation of the guarded
4h candidate-queue strategy. It refuses to start when `dry_run` is false.

## Isolation

| Resource | Strategy 2 path |
|---|---|
| Config | `user_data/config_strategy2_dry.json` |
| Runtime parameters | `user_data/config_strategy2_parameters.json` |
| Candidate queue | `user_data/scan_results/strategy2/daily_trend_candidates.csv` |
| Queue state | `user_data/scan_data/strategy2/queue_state.json` |
| Trade database | `user_data/trades_strategy2_dry.sqlite` |

The strategy 1 config, candidate files, runtime settings, database, and
services are not read or modified.

## Local Start

Generate the first strategy 2 queue:

```bash
.venv/bin/python user_data/scripts/screen_daily_trend.py \
  --config user_data/config_strategy2_dry.json \
  --settings-file user_data/config_strategy2_parameters.json \
  --refresh-universe
```

Start the dry-run bot:

```bash
.venv/bin/freqtrade trade \
  --config user_data/config_strategy2_dry.json
```

The scanner should then run continuously in a separate terminal:

```bash
.venv/bin/python -u user_data/scripts/screen_daily_trend.py \
  --config user_data/config_strategy2_dry.json \
  --settings-file user_data/config_strategy2_parameters.json \
  --loop \
  --retry-delay-seconds 60
```

## Server Installation

The service files are templates only and are not enabled by deployment unless
`deploy/deploy_to_server.sh --systemd` is explicitly used:

```text
deploy/systemd/freqtrade-strategy2-scanner.service
deploy/systemd/freqtrade-strategy2-dry.service
```
