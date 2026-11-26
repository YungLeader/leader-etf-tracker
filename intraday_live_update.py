#!/usr/bin/env python3
"""
盘中实时分时数据更新脚本 (intraday_live_update.py)
功能：每次运行时重新采集今日 ETF + 指数分钟 K 线，更新 intraday_min.json / intraday_min.js
      同步写入 index.html 内嵌的 window.INTRADAY_MIN，并 git push 到 GitHub Pages
用法：
  - 由 cron 在交易时段（9:35~15:10）每 1 分钟调用一次
  - 或者用户点击「立即刷新」时手动触发
"""
import os, sys, json, urllib.request, time, re, subprocess
from datetime import datetime, timedelta

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(REPO_DIR, 'data')
INTRADAY_JSON = os.path.join(DATA_DIR, 'intraday_min.json')
INTRADAY_JS   = os.path.join(DATA_DIR, 'intraday_min.js')
HTML_PATH     = os.path.join(REPO_DIR, 'index.html')
SNAPSHOT_PATH = os.path.join(DATA_DIR, 'snapshot.js')

# 盘中分时历史归档（滚动保留最近 N 个交易日）
INTRADAY_HIST_JSON = os.path.join(DATA_DIR, 'intraday_history.json')
INTRADAY_HIST_JS = os.path.join(DATA_DIR, 'intraday_history.js')
INTRADAY_HIST_KEEP_DAYS = 20


# ---------- 原子写盘工具：与 EOD 脚本保持同一序列化口径（UTF-8 / indent=2 / 末尾换行） ----------
def atomic_write_text(path, text):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_json(path, obj):
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def atomic_write_js(path, var_name, obj):
    atomic_write_text(path, f"window.{var_name} = {json.dumps(obj, ensure_ascii=False, indent=2)};\n")


def archive_intraday_min(intraday_data, keep_days=INTRADAY_HIST_KEEP_DAYS):
    """把当日盘中分时快照写入滚动归档（保留最近 keep_days 个交易日）"""
    hist = {}
    if os.path.exists(INTRADAY_HIST_JSON):
        try:
            with open(INTRADAY_HIST_JSON, 'r', encoding='utf-8') as f:
                hist = json.load(f) or {}
        except Exception as e:
            print(f"[WARN] 分时归档读取失败，将重建: {e}")
            hist = {}
    days = hist.get('days') or {}
    days[intraday_data['date']] = intraday_data
    ordered = sorted(days.keys())[-keep_days:]
    payload = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'keep_days': keep_days,
        'dates': ordered,
        'days': {d: days[d] for d in ordered},
        'notes': '按交易日归档的盘中分时快照，滚动保留最近 keep_days 个交易日，供前端日期回溯与多日对比。',
    }
    atomic_write_json(INTRADAY_HIST_JSON, payload)
    atomic_write_js(INTRADAY_HIST_JS, 'INTRADAY_HISTORY', payload)
    return payload


def _shift_m1(t):
    """新浪1分K时间戳是结束时，转为开始时（-1分钟）"""
    try:
        return (datetime.strptime(t, '%H:%M') - timedelta(minutes=1)).strftime('%H:%M')
    except Exception:
        return t

def compute_and_push():
    now = datetime.now()
    today_dash = now.strftime('%Y-%m-%d')
    hh = now.hour * 60 + now.minute

    # 交易时段外直接退出（设置 FORCE_RUN=1 可绕过）
    import os
    if not os.environ.get('FORCE_RUN') and not ((9*60+30 <= hh <= 11*60+30) or (13*60 <= hh <= 15*60+10)):
        print(f"[{now.strftime('%H:%M')}] 非交易时段，跳过")
        return

    print(f"[{now.strftime('%H:%M:%S')}] 开始采集分时数据 ...")

    # 读取持仓快照
    with open(SNAPSHOT_PATH, 'r', encoding='utf-8') as f:
        content = f.read()
    snapshot = json.loads(content.split('window.SNAPSHOT = ')[1].rstrip().rstrip(';'))
    items = snapshot.get('items', [])
    core_items = [it for it in items if it.get('group') == 'core']

    headers = {'User-Agent': 'Mozilla/5.0'}

    def fetch_min_kline(symbol, datalen=250):
        url = f'https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol={symbol}&scale=1&ma=no&datalen={datalen}'
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as resp:
            return json.loads(resp.read().decode('gbk'))

    # 1. 上证指数分时
    sh_raw = fetch_min_kline('sh000001')
    sh_today = [x for x in sh_raw if x['day'].startswith(today_dash)
                and x['day'][11:16] < '15:00']
    if not sh_today:
        sh_today = sh_raw[-240:]

    sh_times_raw = [x['day'][11:16] for x in sh_today]
    # 只对 <= 14:57 的原始时间做 shift，14:58 保持原样防止与14:57冲突
    sh_times = [_shift_m1(t) if t <= '14:57' else t for t in sh_times_raw]
    # 去重：保留每个时间槽最后一条K线
    seen, dedup_idx = set(), []
    for i in range(len(sh_times) - 1, -1, -1):
        if sh_times[i] not in seen:
            seen.add(sh_times[i])
            dedup_idx.append(i)
    dedup_idx = sorted(dedup_idx)
    sh_today = [sh_today[i] for i in dedup_idx]
    sh_times = [sh_times[i] for i in dedup_idx]
    sh_pts   = [float(x['close']) for x in sh_today]
    n_pts    = len(sh_times)

    if n_pts == 0:
        print("[WARN] 上证指数分时数据为空，跳出")
        return

    prev_close_sh = float(sh_today[0]['open']) if sh_today else sh_pts[0]

    # 2. 创业板指分时
    try:
        cy_raw   = fetch_min_kline('sz399006')
        cy_today = [x for x in cy_raw if x['day'].startswith(today_dash)
                    and x['day'][11:16] < '15:00']
        cy_map   = {_shift_m1(x['day'][11:16]): float(x['close']) for x in cy_today}
        cy_pts   = [cy_map.get(t, float(cy_today[-1]['close']) if cy_today else 2426.08) for t in sh_times]
        prev_close_cy = float(cy_today[0]['open']) if cy_today else 2509.43
    except Exception as e:
        print(f"[WARN] 创业板指降级: {e}")
        cy_pts = [2426.08] * n_pts
        prev_close_cy = 2509.43

    # 3. ETF 分钟成交额
    etf_series = {}
    for it in items:
        tc = it['tcode']
        try:
            raw = fetch_min_kline(tc)
            m_map = {_shift_m1(x['day'][11:16]): float(x['amount'])/1e8
                     for x in raw if x['day'].startswith(today_dash)}
            etf_series[tc] = [m_map.get(t, 0.0) for t in sh_times]
        except Exception:
            etf_series[tc] = [0.0] * n_pts
        time.sleep(0.01)

    def compute_scope(target_items):
        tcodes  = [it['tcode'] for it in target_items]
        min_amt = [round(sum(etf_series.get(tc, [0.0]*n_pts)[i] for tc in tcodes), 2)
                   for i in range(n_pts)]
        cum_amt = []
        cur = 0.0
        for v in min_amt:
            cur += v
            cum_amt.append(round(cur, 2))
        tot_today  = cum_amt[-1] if cum_amt else 0.0
        tot_avg20  = round(sum(it.get('avg20_yi', 0) for it in target_items), 2)
        base_cum   = [round(tot_avg20 * ((i+1)/float(n_pts)), 2) for i in range(n_pts)]
        peak_val   = max(min_amt) if min_amt else 0.0
        peak_i     = min_amt.index(peak_val) if min_amt else 0
        peak_t     = sh_times[peak_i] if sh_times else '09:30'
        return {
            "count": len(target_items),
            "total_today_amount": tot_today,
            "total_avg20_amount": tot_avg20,
            "ratio": round(tot_today / tot_avg20, 2) if tot_avg20 > 0 else 1.0,
            "peak_time": peak_t, "peak_amount": peak_val,
            "min_amount": min_amt, "cum_amount": cum_amt, "base_cum_amount": base_cum
        }

    scope_20 = compute_scope(core_items)
    scope_26 = compute_scope(items)

    # 裁剪尾部指数有、ETF成交额为0的空柱
    tail_cut = 0
    while (tail_cut < len(scope_26['min_amount'])
           and scope_26['min_amount'][-(tail_cut+1)] == 0.0):
        tail_cut += 1
    if tail_cut > 0:
        keep = len(sh_times) - tail_cut
        sh_times = sh_times[:keep]
        sh_pts   = sh_pts[:keep]
        cy_pts   = cy_pts[:keep]
        for sc in (scope_20, scope_26):
            sc['min_amount']      = sc['min_amount'][:keep]
            sc['cum_amount']      = sc['cum_amount'][:keep]
            sc['base_cum_amount'] = sc['base_cum_amount'][:keep]

    intraday_data = {
        "date": today_dash,
        "prev_close": round(prev_close_sh, 2),
        "times": sh_times,
        "sh_points": sh_pts,
        "cy_points": cy_pts,
        "prev_close_cy": prev_close_cy,
        "scope_20": scope_20,
        "scope_26": scope_26
    }

    # 4. 写 JSON 文件（原子写 + 与 EOD 统一 indent=2 口径）
    atomic_write_json(INTRADAY_JSON, intraday_data)
    atomic_write_js(INTRADAY_JS, 'INTRADAY_MIN', intraday_data)

    # 4b. 归档当日盘中分时（本地滚动 20 交易日；不随盘中 push 上传，由 EOD 统一提交）
    try:
        archive_intraday_min(intraday_data)
    except Exception as e:
        print(f"[WARN] 分时历史归档失败: {e}")

    print(f"✅ 分时数据写入完成: {n_pts}条, 累计{scope_20['total_today_amount']:.1f}亿(20只)")

    # 5. Git push（只推 data/ 下的分钟数据，不更新 index.html）
    os.chdir(REPO_DIR)
    env = os.environ.copy()
    env.setdefault('https_proxy', 'http://127.0.0.1:2080')
    env.setdefault('http_proxy', 'http://127.0.0.1:2080')
    env['PATH'] = f"/opt/homebrew/bin:/usr/local/bin:{env.get('PATH', '')}"
    try:
        subprocess.run(['git', 'add', 'data/intraday_min.json', 'data/intraday_min.js'],
                       check=True, capture_output=True, text=True)
        staged = subprocess.run(['git', 'diff', '--cached', '--name-only'],
                                capture_output=True, text=True)
        if not staged.stdout.strip():
            print("⏭️  分钟数据无变化，跳过 push")
            return
        subprocess.run(['git', 'commit', '-m',
                        f'live: intraday update {now.strftime("%H:%M")} n={n_pts}'],
                       check=True, capture_output=True, text=True)
        push_res = subprocess.run(['git', 'push', 'origin', 'main'],
                                  env=env, timeout=120, capture_output=True, text=True)
        if push_res.returncode == 0:
            print("✅ GitHub Pages 已推送")
        else:
            print(f"[ERROR] GitHub Pages 推送失败 (code={push_res.returncode})")
            print(f"        stderr: {(push_res.stderr or '').strip()[:500]}")
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] 盘中 Git 提交/推送失败: {e}")
        print(f"        stderr: {(getattr(e, 'stderr', '') or '').strip()[:500]}")

if __name__ == '__main__':
    compute_and_push()
