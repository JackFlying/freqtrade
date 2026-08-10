#!/usr/bin/env bash

set -Eeuo pipefail

SERVER="${SERVER:-root@45.76.192.137}"
REMOTE_DIR="${REMOTE_DIR:-/opt/freqtrade}"

SYNC_CONFIG=false
SYNC_DEPS=false
SYNC_CORE=false
SYNC_SYSTEMD=false
SYNC_NGINX=false
RESTART_SERVICES=true
DRY_RUN=false

usage() {
    cat <<'EOF'
Usage: deploy/deploy_to_server.sh [options]

增量同步本地 Freqtrade 代码，不覆盖服务器运行数据。

Options:
  --config       同步 user_data/config_scan.json（服务器会保留备份）。
  --deps         重新安装服务器 Python 依赖。
  --core         同步 Freqtrade 框架源码和项目元数据。
  --systemd      更新 systemd 服务文件。
  --nginx        更新并重载 Nginx 配置。
  --no-restart   只同步文件，不重启服务。
  --dry-run      仅预览文件变化。
  -h, --help     显示帮助。

Environment:
  SERVER         SSH 目标（默认 root@45.76.192.137）。
  REMOTE_DIR     服务器项目目录（默认 /opt/freqtrade）。
EOF
}

while (($#)); do
    case "$1" in
        --config) SYNC_CONFIG=true ;;
        --deps) SYNC_DEPS=true ;;
        --core) SYNC_CORE=true ;;
        --systemd) SYNC_SYSTEMD=true ;;
        --nginx) SYNC_NGINX=true ;;
        --no-restart) RESTART_SERVICES=false ;;
        --dry-run) DRY_RUN=true ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "未知参数：$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

for command in ssh rsync; do
    command -v "$command" >/dev/null || {
        echo "缺少命令：$command" >&2
        exit 1
    }
done

echo "[1/4] 本地检查"
"$ROOT_DIR/.venv/bin/python" -m compileall -q \
    user_data/dashboard user_data/scripts user_data/strategies

if command -v node >/dev/null; then
    node --check user_data/dashboard/static/app.js
fi

if "$SYNC_CONFIG"; then
    "$ROOT_DIR/.venv/bin/freqtrade" show-config \
        -c user_data/config_scan.json >/dev/null
fi

# Reuse one SSH connection. The server is about 125 ms away and a fresh SSH
# handshake takes several seconds, so multiplexing saves most deployment time.
CONTROL_PATH="/tmp/freqtrade-deploy-${UID}-%C"
SSH_ARGS=(
    -o BatchMode=yes
    -o ConnectTimeout=10
    -o ControlMaster=auto
    -o ControlPersist=300
    -o "ControlPath=$CONTROL_PATH"
)

ssh "${SSH_ARGS[@]}" "$SERVER" \
    "test -d '$REMOTE_DIR' && test -x '$REMOTE_DIR/.venv/bin/python'"

REMOTE_TRADE_WAS_RUNNING="$(
    ssh "${SSH_ARGS[@]}" "$SERVER" \
        "set -a; test ! -f /etc/freqtrade.env || . /etc/freqtrade.env; set +a; \
        cd '$REMOTE_DIR' && .venv/bin/freqtrade-client \
        -c user_data/config_scan.json show_config 2>/dev/null || true" \
    | grep -q '"state": "running"' && echo true || echo false
)"

if "$SYNC_CONFIG" && ! "$DRY_RUN"; then
    ssh "${SSH_ARGS[@]}" "$SERVER" \
        "cp '$REMOTE_DIR/user_data/config_scan.json' \
        \"$REMOTE_DIR/user_data/config_scan.json.bak-\$(date +%Y%m%d-%H%M%S)\""
fi

SOURCES=(
    user_data/dashboard/
    user_data/scripts/
    user_data/strategies/
)
if "$SYNC_CORE" || "$SYNC_DEPS"; then
    SOURCES+=(freqtrade/ pyproject.toml requirements.txt)
fi
"$SYNC_CONFIG" && SOURCES+=(user_data/config_scan.json)
"$SYNC_SYSTEMD" && SOURCES+=(deploy/systemd/)
"$SYNC_NGINX" && SOURCES+=(deploy/nginx/)

RSYNC_ARGS=(
    -azR
    --itemize-changes
    --out-format='%i %n%L'
    --exclude='__pycache__/'
    --exclude='*.pyc'
    --exclude='*.md'
    -e "ssh -o BatchMode=yes -o ControlPath=$CONTROL_PATH"
)
"$DRY_RUN" && RSYNC_ARGS+=(--dry-run)

echo "[2/4] 单次增量同步"
# Do not use --delete. The server has generated FreqUI assets and runtime files
# that are intentionally absent from the local source tree.
SYNC_OUTPUT="$(
    rsync "${RSYNC_ARGS[@]}" "${SOURCES[@]}" "$SERVER:$REMOTE_DIR/"
)"
[[ -n "$SYNC_OUTPUT" ]] && printf '%s\n' "$SYNC_OUTPUT"

if "$DRY_RUN"; then
    echo "预览完成，服务器未修改。"
    exit 0
fi

# Only transferred files affect restart decisions. Directory timestamp changes
# are ignored, otherwise every deployment would restart all services.
CHANGED_FILES="$(printf '%s\n' "$SYNC_OUTPUT" | awk '$1 ~ /^[<>]f|^cL/ {print $2}')"

CORE_CHANGED=false
DASHBOARD_CHANGED=false
SCANNER_CHANGED=false
STRATEGY_CHANGED=false

grep -Eq '^(freqtrade/|pyproject\.toml$|requirements\.txt$)' \
    <<<"$CHANGED_FILES" && CORE_CHANGED=true || true
grep -q '^user_data/dashboard/' \
    <<<"$CHANGED_FILES" && DASHBOARD_CHANGED=true || true
grep -q '^user_data/scripts/' \
    <<<"$CHANGED_FILES" && SCANNER_CHANGED=true || true
grep -q '^user_data/strategies/' \
    <<<"$CHANGED_FILES" && STRATEGY_CHANGED=true || true

if [[ -z "$CHANGED_FILES" ]] \
    && ! "$SYNC_DEPS" && ! "$SYNC_SYSTEMD" && ! "$SYNC_NGINX" \
    && ! "$SYNC_CONFIG" && ! "$SYNC_CORE"; then
    echo "服务器已是最新版本，无需重启。"
    exit 0
fi

RESTART_TRADE=false
RESTART_SCANNER=false
RESTART_DASHBOARD=false

if "$CORE_CHANGED"; then
    RESTART_TRADE=true
    RESTART_SCANNER=true
    RESTART_DASHBOARD=true
fi
"$DASHBOARD_CHANGED" && RESTART_DASHBOARD=true
"$SCANNER_CHANGED" && RESTART_SCANNER=true
"$STRATEGY_CHANGED" && RESTART_TRADE=true
"$SYNC_CONFIG" && {
    RESTART_TRADE=true
    RESTART_SCANNER=true
    RESTART_DASHBOARD=true
}
"$SYNC_DEPS" && {
    RESTART_TRADE=true
    RESTART_SCANNER=true
    RESTART_DASHBOARD=true
}
"$SYNC_SYSTEMD" && {
    RESTART_TRADE=true
    RESTART_SCANNER=true
    RESTART_DASHBOARD=true
}

echo "[3/4] 应用变更"
REMOTE_COMMAND=$(cat <<EOF
set -Eeuo pipefail
cd '$REMOTE_DIR'

restart_trade=$RESTART_TRADE
restart_scanner=$RESTART_SCANNER
restart_dashboard=$RESTART_DASHBOARD
restart_services=$RESTART_SERVICES
sync_config=$SYNC_CONFIG
sync_deps=$SYNC_DEPS
sync_systemd=$SYNC_SYSTEMD
sync_nginx=$SYNC_NGINX

set -a
test ! -f /etc/freqtrade.env || . /etc/freqtrade.env
set +a

trade_was_running=$REMOTE_TRADE_WAS_RUNNING

if \$sync_deps; then
    nice -n 15 .venv/bin/pip install -e .
fi

if \$sync_systemd; then
    install -m 0644 deploy/systemd/freqtrade-trade.service \
        /etc/systemd/system/freqtrade-trade.service
    install -m 0644 deploy/systemd/freqtrade-scanner.service \
        /etc/systemd/system/freqtrade-scanner.service
    install -m 0644 deploy/systemd/freqtrade-dashboard.service \
        /etc/systemd/system/freqtrade-dashboard.service
    systemctl daemon-reload
fi

if \$sync_nginx; then
    install -m 0644 deploy/nginx/freqtrade-sites.conf \
        /etc/nginx/sites-available/freqtrade-sites
    ln -sfn /etc/nginx/sites-available/freqtrade-sites \
        /etc/nginx/sites-enabled/freqtrade-sites
    nginx -t
    systemctl reload nginx
fi

services=()
if \$restart_services; then
    \$restart_scanner && services+=(freqtrade-scanner)
    \$restart_dashboard && services+=(freqtrade-dashboard)
    \$restart_trade && services+=(freqtrade-trade)
    ((\${#services[@]})) && systemctl restart "\${services[@]}"
fi

wait_http() {
    local url=\$1
    local attempts=\$2
    for ((i=0; i<attempts; i++)); do
        curl -fsS "\$url" >/dev/null 2>&1 && return 0
        sleep 1
    done
    return 1
}

if \$restart_services; then
    \$restart_dashboard && wait_http http://127.0.0.1:8083/api/health 180
    \$restart_trade && wait_http http://127.0.0.1:8081/api/v1/ping 180

    if \$restart_trade && \$trade_was_running; then
        .venv/bin/freqtrade-client -c user_data/config_scan.json start >/dev/null
        for ((i=0; i<20; i++)); do
            .venv/bin/freqtrade-client -c user_data/config_scan.json \
                show_config 2>/dev/null | grep -q '"state": "running"' && break
            sleep 1
        done
    fi
fi

systemctl is-active --quiet freqtrade-trade
systemctl is-active --quiet freqtrade-scanner
systemctl is-active --quiet freqtrade-dashboard
curl -fsS http://127.0.0.1:8081/api/v1/ping
printf '\n'
curl -fsS http://127.0.0.1:8083/api/health
printf '\n'
EOF
)

ssh "${SSH_ARGS[@]}" "$SERVER" "bash -s" <<<"$REMOTE_COMMAND"

echo "[4/4] 部署完成：$SERVER:$REMOTE_DIR"
