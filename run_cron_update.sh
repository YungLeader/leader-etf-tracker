#!/bin/bash
# 每日交易日 15:35 自动更新国家队旺旺队 ETF 看板数据并推送到 GitHub Pages

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
# 代理改为可选：仅当 127.0.0.1:2080 存活时启用，否则直连
# （历史坑：代理未启动时 export 死代理会让采集请求全部失败）
if (exec 3<>/dev/tcp/127.0.0.1/2080) 2>/dev/null; then
  exec 3>&- 2>/dev/null
  export https_proxy="http://127.0.0.1:2080"
  export http_proxy="http://127.0.0.1:2080"
else
  unset https_proxy http_proxy HTTPS_PROXY HTTP_PROXY
fi

LOG_DIR="/Users/leader/.openclaw/workspace-stock-agent/tools/leader-etf-tracker-repo/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/cron_update_$(date +\%Y\%m\%d).log"

echo "=== [$(date '+%Y-%m-%d %H:%M:%S')] 启动 ETF 看板自动更新 ===" >> "$LOG_FILE"
/Users/leader/.openclaw/workspace-stock-agent/tdx_env/bin/python3 /Users/leader/.openclaw/workspace-stock-agent/tools/leader-etf-tracker-repo/auto_eod_update.py >> "$LOG_FILE" 2>&1
echo "=== [$(date '+%Y-%m-%d %H:%M:%S')] 更新结束 ===" >> "$LOG_FILE"
