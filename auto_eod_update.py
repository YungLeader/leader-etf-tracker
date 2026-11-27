#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国家队旺旺队 ETF 看板收盘全自动更新引擎 (Auto EOD Pipeline 终极硬化版)
1. 抓取今日 24 只核心 ETF 真实收盘行情（现价、涨跌幅、成交额、内外盘、换手率等）
2. 抓取今日沪深 300 收盘点位，追加至 macro_daily_history.json 宏观大底曲线
3. 滑动更新 30 日历史成交底座，重新计算过去 20 日平均成交额（avg20_yi）
4. 抓取今日上证指数与 24 只 ETF 原生 1 分钟分时成交，分别物理计算 scope_20 与 scope_24 独立数据集
5. 自动重新编译重绘 index.html 中的宏观大底 SVG 路径、最新 X 轴日期刻度、Badge 比重以及内嵌分时底座
6. 同步写盘到仓库，并通过系统代理自动 git commit 和 git push 到 GitHub Pages（使用免密 Token URL）！
"""

import os
import sys
import json
import datetime
import urllib.request
import subprocess
import re
import time

# ---- 代理策略：存活探测 + 直连兜底 ----
# 历史坑：本地代理(127.0.0.1:2080)未启动时环境变量仍指向死代理，
# 导致 urllib 请求全部 Connection refused，采集静默 100% 失败。
PROXY_URL = 'http://127.0.0.1:2080'


def _proxy_alive(url=PROXY_URL, timeout=0.5):
    try:
        import socket
        host, _, port = url.split('//')[-1].partition(':')
        with socket.create_connection((host, int(port or 80)), timeout=timeout):
            return True
    except Exception:
        return False


PROXY_OK = _proxy_alive()


def apply_proxy(env):
    """代理存活则注入，否则彻底清除代理变量，保证直连。"""
    if PROXY_OK:
        env['https_proxy'] = PROXY_URL
        env['http_proxy'] = PROXY_URL
    else:
        for k in ('https_proxy', 'http_proxy', 'HTTPS_PROXY', 'HTTP_PROXY',
                  'all_proxy', 'ALL_PROXY'):
            env.pop(k, None)
    return env


# 进程级生效：urllib 默认读取 os.environ
apply_proxy(os.environ)

REPO_DIR = '/Users/leader/.openclaw/workspace-stock-agent/tools/leader-etf-tracker-repo'
SRC_DIR = '/Users/leader/.openclaw/workspace-stock-agent/tools/etf-tracker'
SKILL_DIR = '/Users/leader/.openclaw/workspace-stock-agent/skills/national-team-etf-tracker'
DATA_DIR = os.path.join(REPO_DIR, 'data')
SNAPSHOT_PATH = os.path.join(DATA_DIR, 'snapshot.js')
MACRO_PATH = os.path.join(DATA_DIR, 'macro_daily_history.json')
INTRADAY_JSON = os.path.join(DATA_DIR, 'intraday_min.json')
INTRADAY_JS = os.path.join(DATA_DIR, 'intraday_min.js')
HTML_PATH = os.path.join(REPO_DIR, 'index.html')

# ================= 新增数据层产物（宽基净流入矩阵 + 盘中分时历史归档） =================
WIDEBASE_JSON = os.path.join(DATA_DIR, 'widebase_netflow.json')
WIDEBASE_JS = os.path.join(DATA_DIR, 'widebase_netflow.js')
INTRADAY_HIST_JSON = os.path.join(DATA_DIR, 'intraday_history.json')
INTRADAY_HIST_JS = os.path.join(DATA_DIR, 'intraday_history.js')
INTRADAY_HIST_KEEP_DAYS = 20

# 大类宽基列定义（顺序即前端展示顺序）
WIDEBASE_COLUMNS = [
    ('hs300', '沪深300'),
    ('csi1000', '中证1000'),
    ('csi500', '中证500'),
    ('star50', '科创50'),
    ('csia500', '中证A500'),
    ('chinext', '创业板指'),
    ('sse50', '上证50'),
    ('other', '其他宽基'),
]

# 跟踪指数 -> 宽基大类
INDEX_TO_COLUMN = {
    '沪深300': 'hs300',
    '中证1000': 'csi1000',
    '中证500': 'csi500',
    '科创50': 'star50',
    '中证A500': 'csia500',
    '创业板指': 'chinext',
    '上证50': 'sse50',
    '上证180': 'other',
    '上证180金融': 'other',
    '中证800': 'other',
    '深证100': 'other',
}


# ---------- 原子写盘工具：统一序列化格式（UTF-8 / indent=2 / 末尾换行 / 临时文件+rename） ----------
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


def fetch_daily_closes(tcode, datalen=400):
    """拉取单只 ETF 日线收盘价，返回 {'YYYYMMDD': close}"""
    url = ('https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData'
           f'?symbol={tcode}&scale=240&ma=no&datalen={datalen}')
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    out = {}
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read().decode('gbk'))
        for x in raw:
            d = str(x.get('day', ''))[:10].replace('-', '')
            try:
                out[d] = float(x.get('close', 0))
            except Exception:
                continue
    except Exception as e:
        print(f"  [WARN] {tcode} 日线拉取失败: {e}")
    return out


def _shape_buckets():
    """日内成交额分布的时间桶：09:35-11:30 与 13:05-15:00，5 分钟一格，共 48 点"""
    out = []
    for start, end in ((575, 690), (785, 900)):
        t = start
        while t <= end:
            out.append(f"{t // 60:02d}:{t % 60:02d}")
            t += 5
    return out


def fetch_tx_minute_cum(tcode):
    """腾讯分时接口：返回 [(hhmm, 累计成交额), ...]，最后一行为收盘集合竞价后的全天累计额"""
    url = f'https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={tcode}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=8) as resp:
        j = json.loads(resp.read().decode('utf-8', 'ignore'))
    node = (j.get('data') or {}).get(tcode) or {}
    rows = (node.get('data') or {}).get('data') or []
    out = []
    for r in rows:
        p = str(r).split()
        if len(p) < 4:
            continue
        try:
            out.append((p[0], float(p[3])))
        except Exception:
            continue
    return out


def refresh_item_shapes(items):
    """重算每只 ETF 的当日累计成交额占比序列 items[].shape（48 点）

    原 shape 为早期静态样本、长期未随交易日更新，且新纳入观察池的标的直接没有该字段，
    前端「日内成交额分布」列会显示陈旧图形或「无日内分布」。此处改为每日收盘后按
    腾讯分时接口的累计成交额重算，保证该列与当日真实分时一致。
    """
    buckets = _shape_buckets()
    ok, fail = 0, []
    for it in items:
        code, tc = it.get('code'), it.get('tcode')
        if not code or not tc:
            continue
        try:
            rows = fetch_tx_minute_cum(tc)
            if len(rows) < 10:
                fail.append(code)
                continue
            total = rows[-1][1]
            if total <= 0:
                fail.append(code)
                continue
            seq, j, last = [], 0, 0.0
            for b in buckets:
                key = b.replace(':', '')
                while j < len(rows) and rows[j][0] <= key:
                    last = rows[j][1]
                    j += 1
                if b == '15:00':
                    last = total   # 收盘集合竞价并入 15:00
                ratio = min(1.0, max(0.0, last / total))
                seq.append([b, round(ratio, 5)])
            it['shape'] = seq
            ok += 1
        except Exception:
            fail.append(code)
        time.sleep(0.02)
    print(f"✅ 日内成交额分布(shape) 刷新: 成功 {ok} 只" + (f"，失败 {len(fail)} 只 {fail}" if fail else ""))
    return ok, fail


def build_widebase_netflow(items, shares_history, today_str):
    """生成 交易日 × 大类宽基 的净流入矩阵（净流入 = 份额变化(亿份) × 当日收盘价(元)，单位亿元）"""
    code_col = {}
    for it in items:
        code_col[it['code']] = INDEX_TO_COLUMN.get(it.get('index', ''), 'other')

    price_hist = {}
    for it in items:
        price_hist[it['code']] = fetch_daily_closes(it['tcode'])
        time.sleep(0.01)

    dates = sorted(shares_history.keys())
    rows = []
    prev_date, prev_row = None, None
    for d in dates:
        cur = shares_history.get(d) or {}
        if prev_row is None:
            prev_date, prev_row = d, cur
            continue
        acc = {k: 0.0 for k, _ in WIDEBASE_COLUMNS}
        col_compared = {k: 0 for k, _ in WIDEBASE_COLUMNS}
        compared = 0
        changed = 0
        missing_price = 0
        for code, s in cur.items():
            if code not in prev_row or code not in code_col:
                continue
            compared += 1
            col = code_col[code]
            col_compared[col] += 1
            ds = s - prev_row[code]
            # 份额变动阈值 0.01 亿份（100 万份），过滤浮点噪声，仅用于“份额源是否日更”的判定
            if abs(ds) < 0.01:
                continue
            changed += 1
            px = price_hist.get(code, {}).get(d) or price_hist.get(code, {}).get(prev_date)
            if not px:
                missing_price += 1
                continue
            acc[col] += ds * px
        prev_date, prev_row = d, cur
        if compared == 0:
            continue
        stale = (changed / float(compared)) < 0.30
        row = {'date': d}
        if stale:
            # 份额源未日更：整行置空，避免产生假的净流入跳变
            row['stale'] = True
            for k, _ in WIDEBASE_COLUMNS:
                row[k] = None
            row['total'] = None
        else:
            row['stale'] = False
            for k, _ in WIDEBASE_COLUMNS:
                # 该大类当日无任何可比标的（如观察池标的尚未纳入历史序列）时置 null，不用 0 冒充
                row[k] = round(acc[k], 2) if col_compared[k] else None
            row['total'] = round(sum(v for v in (row[k] for k, _ in WIDEBASE_COLUMNS) if v is not None), 2)
        if missing_price:
            row['missing_price_count'] = missing_price
        rows.append(row)

    first_date = {}
    for row in rows:
        if row.get('stale'):
            continue
        for k, _ in WIDEBASE_COLUMNS:
            if k not in first_date and row.get(k) is not None:
                first_date[k] = row['date']

    payload = {
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'trade_date': today_str,
        'unit': '亿元',
        'columns': [{'key': k, 'label': v} for k, v in WIDEBASE_COLUMNS],
        'rows': list(reversed(rows)),   # 最新交易日在最前，与看板表格一致
        'count': len(rows),
        'column_start': first_date,     # 各宽基列首个有效数据日（null 表示该列历史不可得）
        'notes': ('净流入 = 当日份额 − 上一交易日份额（亿份）× 当日收盘价（元）；份额源为 snapshot.shares_history，'
                  '收盘价源为新浪日线；某日 ≥70% 标的份额变动不足 0.01 亿份时判定份额源未日更，该行数值置空并标 stale=true；'
                  '某列当日无任何可比标的时该单元格为 null（前端显示「—」）；'
                  '“其他宽基”含上证180、上证180金融、中证800、深证100；中证A500 由观察池 512050/159338/159339 聚合，'
                  '其份额序列已尽量回溯补齐，仍无前值可比的交易日为 null（前端显示「—」），属数据可得性限制而非 0 流入。'),
    }
    atomic_write_json(WIDEBASE_JSON, payload)
    atomic_write_js(WIDEBASE_JS, 'WIDEBASE_NETFLOW', payload)
    print(f"✅ 宽基净流入矩阵已生成: {payload['count']} 个交易日 × {len(WIDEBASE_COLUMNS)} 个宽基大类")
    return payload


def archive_intraday_min(intraday_data, keep_days=INTRADAY_HIST_KEEP_DAYS):
    """把当日分时快照写入滚动归档，保留最近 keep_days 个交易日"""
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
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'keep_days': keep_days,
        'dates': ordered,
        'days': {d: days[d] for d in ordered},
        'notes': '按交易日归档的盘中分时快照，滚动保留最近 keep_days 个交易日，供前端日期回溯与多日对比。',
    }
    atomic_write_json(INTRADAY_HIST_JSON, payload)
    atomic_write_js(INTRADAY_HIST_JS, 'INTRADAY_HISTORY', payload)
    print(f"✅ 分时历史归档更新: 保留 {len(ordered)} 个交易日 ({ordered[0] if ordered else '-'} ~ {ordered[-1] if ordered else '-'})")
    return payload


def fetch_quotes(tcodes):
    url = f"https://qt.gtimg.cn/q={','.join(tcodes)}"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode('gbk')
            quotes = {}
            for line in content.strip().split(';'):
                if line.strip():
                    parts = line.split('=')
                    tc = parts[0].replace('v_', '').strip()
                    val = parts[1].strip('\"')
                    d = val.split('~')
                    if len(d) > 38:
                        def parse_num(idx):
                            try: return float(d[idx])
                            except: return 0.0
                        quotes[tc] = {
                            'tcode': tc,
                            'name': d[1],
                            'code': d[2],
                            'price': parse_num(3),
                            'prev_close': parse_num(4),
                            'volume_lots': parse_num(6),
                            'outer_lots': parse_num(7),
                            'inner_lots': parse_num(8),
                            'pct': parse_num(32),
                            'amount_wan': parse_num(37),
                            'amount_yi': parse_num(37) / 10000.0,
                            'turnover': parse_num(38),
                            'mktcap_yi': parse_num(45),
                            'shares': parse_num(72) if len(d) > 72 else 0.0,
                            'ts': d[30] if len(d) > 30 else '',
                            'date': d[30][:8] if len(d) > 30 else ''
                        }
            return quotes
    except Exception as e:
        print(f"[ERROR] 抓取实时行情异常: {e}")
        return {}

def fetch_hs300_close():
    url = "https://qt.gtimg.cn/q=sh000300"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            content = resp.read().decode('gbk')
            val = content.split('=')[1].strip('\"')
            d = val.split('~')
            return float(d[3])
    except Exception as e:
        print(f"[ERROR] 抓取沪深300收盘价异常: {e}")
        return None



def fetch_avg_price():
    """抓取A股平均股价（来自乐咕乐股），用于26只ETF图表左轴
    API: /stockdata/marketData/getHistoryData?marketId=10000, token=MD5(YYYY-MM-DD)
    """
    import hashlib, datetime as _dt
    today_str = _dt.date.today().strftime('%Y-%m-%d')
    token = hashlib.md5(today_str.encode()).hexdigest()
    url = f'https://legulegu.com/stockdata/marketData/getHistoryData?marketId=10000&dataLength=5&token={token}'
    headers_lg = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Referer': 'https://legulegu.com/stockdata/market-analysis-average-price',
        'Accept': 'application/json',
    }
    req = urllib.request.Request(url, headers=headers_lg)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode('utf-8'))
            items_list = d.get('aSharesDataList', [])
            if items_list:
                return float(items_list[0]['close'])  # 最新一日收盘值
            return None
    except Exception as e:
        print(f'[ERROR] 抓取A股平均股价异常: {e}')
        return None


def calc_all26_ratio(items):
    """计算26只ETF当日总市值占全A流通市值比重(%)
    全A流通市值通过4只头部沪深300ETF市值反推"""
    top4_codes = {'510300', '510310', '510330', '159919'}
    top4_mktcap = sum(it.get('mktcap_yi', 0) for it in items if it['code'] in top4_codes)
    total_26_mktcap = sum(it.get('mktcap_yi', 0) for it in items)
    if top4_mktcap > 0:
        # 4只ETF占比基准: 0.783% (每日使用头部4只/全A流通市值反推)
        # 由于ratios_4固定为0.783，全A流通市值 = top4_mktcap/0.00783
        all_a_mktcap = top4_mktcap / 0.00783
        ratio_26 = round(total_26_mktcap / all_a_mktcap * 100, 3)
    else:
        ratio_26 = 0.0
    return ratio_26

def compute_and_save_intraday_min(items, today_dash):
    core_items = [it for it in items if it.get('group') == 'core']
    url_idx = 'https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol=sh000001&scale=1&ma=no&datalen=250'
    req = urllib.request.Request(url_idx, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as resp:
        sh_raw = json.loads(resp.read().decode('gbk'))

    sh_today = [x for x in sh_raw if x['day'].startswith(today_dash)]
    if not sh_today:
        sh_today = sh_raw[-240:]
    # 保留完整交易时段，含收盘集合竞价三分钟（14:57-15:00，新浪只在 15:00 返回竞价成交量）
    sh_today = [x for x in sh_today if x['day'][11:16] <= '15:00']
    sh_times_raw = [x['day'][11:16] for x in sh_today]
    # 新浪1分K时间戳为K线结束时间，减1分钟转为开始时间展示
    # 14:58/14:59/15:00 属收盘集合竞价，统一聚合到 '15:00' 一个时点
    def _shift_m1(t):
        try:
            from datetime import datetime as _dt, timedelta as _td
            return (_dt.strptime(t, '%H:%M') - _td(minutes=1)).strftime('%H:%M')
        except Exception:
            return t

    CLOSE_SLOT = '15:00'

    def _norm_slot(t):
        if t <= '14:57':
            return _shift_m1(t)
        return CLOSE_SLOT

    sh_times = [_norm_slot(t) for t in sh_times_raw]
    # 去重：shift后如有重复时间槽，保留最后一条（时间更晚的那根K线）
    seen, dedup_idx = set(), []
    for i in range(len(sh_times) - 1, -1, -1):
        if sh_times[i] not in seen:
            seen.add(sh_times[i])
            dedup_idx.append(i)
    dedup_idx = sorted(dedup_idx)
    sh_today = [sh_today[i] for i in dedup_idx]
    sh_times = [sh_times[i] for i in dedup_idx]
    sh_pts = [float(x['close']) for x in sh_today]
    n_pts = len(sh_times)

    def fetch_etf_min(tc):
        url = f'https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol={tc}&scale=1&ma=no&datalen=250'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                raw = json.loads(resp.read().decode('gbk'))
                # 时间槽与指数对齐，收盘竞价 14:58/15:00 累加到 15:00，防止成交额被丢弃
                m_map = {}
                for x in raw:
                    if not x['day'].startswith(today_dash):
                        continue
                    slot = _norm_slot(x['day'][11:16])
                    m_map[slot] = m_map.get(slot, 0.0) + float(x['amount']) / 1e8
                return [m_map.get(t, 0.0) for t in sh_times]
        except Exception:
            return [0.0] * n_pts

    etf_min_series = {}
    for it in items:
        tc = it['tcode']
        etf_min_series[tc] = fetch_etf_min(tc)
        time.sleep(0.01)

    def compute_scope(target_items):
        tcodes = [it['tcode'] for it in target_items]
        min_amt = [0.0] * n_pts
        for tc in tcodes:
            s = etf_min_series.get(tc, [0.0]*n_pts)
            for i in range(n_pts):
                min_amt[i] += s[i]
        min_amt = [round(x, 2) for x in min_amt]
        cum_amt = [0.0] * n_pts
        cur = 0.0
        for i in range(n_pts):
            cur += min_amt[i]
            cum_amt[i] = round(cur, 2)
        tot_today = cum_amt[-1]
        tot_avg20 = round(sum(it.get('avg20_yi', 0) for it in target_items), 2)
        base_cum = [round(tot_avg20 * ((i + 1) / float(n_pts)), 2) for i in range(n_pts)]
        peak_val = max(min_amt) if min_amt else 0.0
        peak_i = min_amt.index(peak_val) if min_amt else 0
        peak_t = sh_times[peak_i] if sh_times else '09:30'
        return {
            "count": len(target_items),
            "total_today_amount": tot_today,
            "total_avg20_amount": tot_avg20,
            "ratio": round(tot_today / tot_avg20, 2) if tot_avg20 > 0 else 1.0,
            "peak_time": peak_t,
            "peak_amount": peak_val,
            "min_amount": min_amt,
            "cum_amount": cum_amt,
            "base_cum_amount": base_cum
        }

    # 同时拉取创业板指 (sz399006) 分时数据
    url_cy = 'https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol=sz399006&scale=1&ma=no&datalen=250'
    req_cy = urllib.request.Request(url_cy, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req_cy, timeout=4) as resp:
            cy_raw = json.loads(resp.read().decode('gbk'))
            cy_today = [x for x in cy_raw if x['day'].startswith(today_dash) and x['day'][11:16] <= '15:00']
            cy_map = {_norm_slot(x['day'][11:16]): float(x['close']) for x in cy_today}
            cy_pts = [cy_map.get(t, float(cy_today[-1]['close']) if cy_today else 2426.08) for t in sh_times]
            prev_close_cy = float(cy_today[0]['open']) if cy_today else 2509.43
    except Exception as e:
        print(f"[WARN] 创业板指分时拉取降级: {e}")
        cy_pts = [2426.08] * n_pts
        prev_close_cy = 2509.43

    scope_20 = compute_scope(core_items)
    scope_26 = compute_scope(items)

    # 裁剪尾部指数有、ETF成交额为0的空柱（收盘集合竞价 15:00 时点永不裁剪，防丢最后一分钟）
    # 从尾部反向扫，连续裁掉 scope_26.min_amount == 0 的条目
    tail_cut = 0
    cut_limit = len(scope_26['min_amount'])
    if sh_times and sh_times[-1] == CLOSE_SLOT:
        cut_limit -= 1
    while (tail_cut < cut_limit
           and scope_26['min_amount'][-(tail_cut+1)] == 0.0):
        tail_cut += 1
    if tail_cut > 0:
        keep = len(sh_times) - tail_cut
        sh_times   = sh_times[:keep]
        sh_pts     = sh_pts[:keep]
        cy_pts     = cy_pts[:keep]
        for sc in (scope_20, scope_26):
            sc['min_amount']      = sc['min_amount'][:keep]
            sc['cum_amount']      = sc['cum_amount'][:keep]
            sc['base_cum_amount'] = sc['base_cum_amount'][:keep]
        print(f'  裁剪尾部空柱 {tail_cut} 条，保留 {keep} 条')

    intraday_data = {
        "date": today_dash,
        "prev_close": round(sh_pts[0], 2) if sh_pts else 3663.72,
        "times": sh_times,
        "sh_points": sh_pts,
        "cy_points": cy_pts,
        "prev_close_cy": prev_close_cy,
        "scope_20": scope_20,
        "scope_26": scope_26
    }

    atomic_write_json(INTRADAY_JSON, intraday_data)
    atomic_write_js(INTRADAY_JS, 'INTRADAY_MIN', intraday_data)

    # 归档当日分时（滚动保留最近 20 个交易日）
    archive_intraday_min(intraday_data)

    return intraday_data

def rebuild_index_html(macro_data, intraday_data, today_dash):
    dates = macro_data['dates']
    pts = macro_data['hs300_points']
    ratios = macro_data['ratios']
    n = len(dates)

    w, h = 1196, 400
    padL, padR, padT, padB = 60, 70, 30, 40
    chartW = w - padL - padR
    chartH = h - padT - padB
    minPt, maxPt = 3000.0, 6000.0
    minRt, maxRt = 0.0, 3.0

    def getX(idx): return padL + (idx / float(n <= 1 and 1 or n - 1)) * chartW
    def getYPt(v): return padT + (1.0 - (v - minPt) / (maxPt - minPt)) * chartH
    def getYRt(v): return padT + (1.0 - (v - minRt) / (maxRt - minRt)) * chartH

    pathPt = f"M {getX(0):.1f} {getYPt(pts[0]):.1f}"
    pathRt = f"M {getX(0):.1f} {getYRt(ratios[0]):.1f}"
    for idx in range(1, n):
        pathPt += f" L {getX(idx):.1f} {getYPt(pts[idx]):.1f}"
        pathRt += f" L {getX(idx):.1f} {getYRt(ratios[idx]):.1f}"

    svgInner = []
    for step in range(7):
        v = step * 0.5
        y = getYRt(v)
        ptVal = int(minPt + (v / 3.0) * (maxPt - minPt))
        svgInner.append(f'<line x1="{padL}" y1="{y:.1f}" x2="{w - padR}" y2="{y:.1f}" stroke="#f0f0f0" stroke-width="1"/>')
        svgInner.append(f'<text x="{padL - 12}" y="{y + 4:.1f}" fill="#005eb8" font-size="12" text-anchor="end" font-family="monospace">{ptVal:,}</text>')
        svgInner.append(f'<text x="{w - padR + 12}" y="{y + 4:.1f}" fill="#ff4713" font-size="12" text-anchor="start" font-family="monospace">{v:.2f}%</text>')

    for idx, d in enumerate(dates):
        if d[5:] == '01-02' or (d[5:] in ['01-03','01-04','01-05','01-06'] and dates[idx-1][:4] != d[:4]):
            x = getX(idx)
            svgInner.append(f'<line x1="{x:.1f}" y1="{h - padB}" x2="{x:.1f}" y2="{h - padB + 6}" stroke="#ccc"/>')
            svgInner.append(f'<text x="{x:.1f}" y="{h - padB + 20}" fill="#888" font-size="12" text-anchor="middle">{d[:4]}-01</text>')

    x_last = getX(n - 1)
    svgInner.append(f'<line x1="{x_last:.1f}" y1="{h - padB}" x2="{x_last:.1f}" y2="{h - padB + 6}" stroke="#ccc"/>')
    svgInner.append(f'<text x="{x_last:.1f}" y="{h - padB + 20}" fill="#888" font-size="12" text-anchor="middle">{dates[-1]}</text>')

    svgInner.append(f'<path d="{pathPt}" fill="none" stroke="#005eb8" stroke-width="2.0" stroke-linejoin="round"/>')
    svgInner.append(f'<path d="{pathRt}" fill="none" stroke="#ff4713" stroke-width="2.6" stroke-linejoin="round"/>')

    peakIdx = ratios.index(2.51) if 2.51 in ratios else 1147
    peakX = getX(peakIdx)
    peakY = getYRt(2.51)
    svgInner.append(f'<circle cx="{peakX:.1f}" cy="{peakY:.1f}" r="5" fill="#ff4713"/>')
    svgInner.append(f'<rect x="{peakX - 25:.1f}" y="{peakY - 26:.1f}" width="50" height="20" rx="4" fill="#ff4713"/>')
    svgInner.append(f'<text x="{peakX:.1f}" y="{peakY - 12:.1f}" fill="#fff" font-size="11" font-weight="bold" text-anchor="middle">2.51%</text>')

    lastX = getX(n - 1)
    lastY = getYRt(ratios[-1])
    svgInner.append(f'<circle cx="{lastX:.1f}" cy="{lastY:.1f}" r="5" fill="#ff4713"/>')
    svgInner.append(f'<rect x="{lastX - 45:.1f}" y="{lastY - 26:.1f}" width="50" height="20" rx="4" fill="#ff4713"/>')
    svgInner.append(f'<text x="{lastX - 20:.1f}" y="{lastY - 12:.1f}" fill="#fff" font-size="11" font-weight="bold" text-anchor="middle">{ratios[-1]}%</text>')
    
    # 注入十字瞄准线与高亮节点 (初始隐藏，由 JS 交互动态驱动)
    svgInner.append('<g id="macroCrosshair" style="display:none;">')
    svgInner.append(f'<line id="macroCrossLine" y1="{padT}" y2="{h - padB}" stroke="#666" stroke-width="1.2" stroke-dasharray="3,3"/>')
    svgInner.append('<circle id="macroHoverPt" r="5" fill="#005eb8" stroke="#fff" stroke-width="2"/>')
    svgInner.append('<circle id="macroHoverRt" r="5" fill="#ff4713" stroke="#fff" stroke-width="2"/>')
    svgInner.append('</g>')

    new_svg = f'<svg id="macroSvg" viewBox="0 0 {w} {h}" style="width:100%;height:{h}px;display:block;">' + ''.join(svgInner) + '</svg>'

    with open(HTML_PATH, 'r', encoding='utf-8') as f:
        html = f.read()

    html = re.sub(r'<svg id="macroSvg"[^>]*>.*?</svg>', new_svg, html, flags=re.DOTALL)
    badge_html = f'<div id="macro-badge" style="background:#fff0ec;color:#ff4713;padding:6px 16px;border-radius:20px;font-weight:800;font-size:14px;border:1px solid #ffd8cf;">\n        当前比重: {ratios[-1]}% (估值大底回升区)\n      </div>'
    html = re.sub(r'<div id="macro-badge"[^>]*>.*?</div>', badge_html, html, flags=re.DOTALL)

    # 同步更新 window.MACRO_DAILY 内嵌 JSON，确保初始加载与 JSON 文件一致
    macro_json_str = json.dumps({
        'dates': dates, 'hs300_points': pts, 'ratios': ratios, 'count': n,
        'avg_price_points': macro_data.get('avg_price_points', []),
        'ratios_all26': macro_data.get('ratios_all26', [])
    }, ensure_ascii=False)
    html = re.sub(r'window\.MACRO_DAILY\s*=\s*\{.*?\};', f'window.MACRO_DAILY = {macro_json_str};', html, flags=re.DOTALL)

    intraday_json_str = json.dumps(intraday_data, ensure_ascii=False)
    if intraday_data:  # 只有有实际数据时才替换，防止空对象覆盖分时数据
        html = re.sub(r'window\.INTRADAY_MIN\s*=\s*\{.*?\};\nwindow\.__curScope', f'window.INTRADAY_MIN = {intraday_json_str};\nwindow.__curScope', html, flags=re.DOTALL)
    html = re.sub(r'<div style="font-size:12px;color:#888;" id="intradayUpdateTime">.*?</div>', f'<div style="font-size:12px;color:#888;" id="intradayUpdateTime">{today_dash} 15:00 收盘锁定</div>', html)

    # ---- 建第二图表 macro26Svg (A股平均股价 + 26只ETF占比) ----
    avg_price_pts = macro_data.get('avg_price_points', [])
    ratios_all26 = macro_data.get('ratios_all26', [])
    n26 = n  # 与 dates 长度相同
    if avg_price_pts and ratios_all26 and len(avg_price_pts) == n26 and len(ratios_all26) == n26:
        # 左轴: A股平均股价 10~35元, 右轴: 26只ETF占比 0~7%
        minAvg, maxAvg = 10.0, 35.0
        minR26, maxR26 = 0.0, 7.0
        padL26, padR26, padT26, padB26 = 70, 80, 30, 40
        cW26 = w - padL26 - padR26
        cH26 = h - padT26 - padB26

        def gX26(idx): return padL26 + (idx / float(n26 <= 1 and 1 or n26 - 1)) * cW26
        def gYAvg(v): return padT26 + (1.0 - (v - minAvg) / (maxAvg - minAvg)) * cH26
        def gYR26(v): return padT26 + (1.0 - (v - minR26) / (maxR26 - minR26)) * cH26

        path26Avg = f"M {gX26(0):.1f} {gYAvg(avg_price_pts[0]):.1f}"
        path26Rt = f"M {gX26(0):.1f} {gYR26(ratios_all26[0]):.1f}"
        for _j in range(1, n26):
            path26Avg += f" L {gX26(_j):.1f} {gYAvg(avg_price_pts[_j]):.1f}"
            path26Rt += f" L {gX26(_j):.1f} {gYR26(ratios_all26[_j]):.1f}"

        si26 = []
        # 8条网格线（平均股价左轴）
        for _step in range(8):
            _avgV = minAvg + _step * (maxAvg - minAvg) / 7.0
            _r26V = minR26 + _step * (maxR26 - minR26) / 7.0
            _y26 = gYAvg(_avgV)
            si26.append(f'<line x1="{padL26}" y1="{_y26:.1f}" x2="{w - padR26}" y2="{_y26:.1f}" stroke="#f0f0f0" stroke-width="1"/>')
            si26.append(f'<text x="{padL26 - 12}" y="{_y26 + 4:.1f}" fill="#005eb8" font-size="12" text-anchor="end" font-family="monospace">{_avgV:.0f}元</text>')
            si26.append(f'<text x="{w - padR26 + 12}" y="{_y26 + 4:.1f}" fill="#28a745" font-size="12" text-anchor="start" font-family="monospace">{_r26V:.1f}%</text>')

        for _idx, _d in enumerate(dates):
            if _d[5:] == '01-02' or (_d[5:] in ['01-03','01-04','01-05','01-06'] and dates[_idx-1][:4] != _d[:4]):
                _xd = gX26(_idx)
                si26.append(f'<line x1="{_xd:.1f}" y1="{h - padB26}" x2="{_xd:.1f}" y2="{h - padB26 + 6}" stroke="#ccc"/>')
                si26.append(f'<text x="{_xd:.1f}" y="{h - padB26 + 20}" fill="#888" font-size="12" text-anchor="middle">{_d[:4]}-01</text>')

        _xL26 = gX26(n26 - 1)
        si26.append(f'<line x1="{_xL26:.1f}" y1="{h - padB26}" x2="{_xL26:.1f}" y2="{h - padB26 + 6}" stroke="#ccc"/>')
        si26.append(f'<text x="{_xL26:.1f}" y="{h - padB26 + 20}" fill="#888" font-size="12" text-anchor="middle">{dates[-1]}</text>')

        si26.append(f'<path d="{path26Avg}" fill="none" stroke="#005eb8" stroke-width="2.0" stroke-linejoin="round"/>')
        si26.append(f'<path d="{path26Rt}" fill="none" stroke="#28a745" stroke-width="2.6" stroke-linejoin="round"/>')

        # 最新圆点标注
        _lx26 = gX26(n26 - 1)
        _lyAvg = gYAvg(avg_price_pts[-1])
        _lyRt = gYR26(ratios_all26[-1])
        si26.append(f'<circle cx="{_lx26:.1f}" cy="{_lyAvg:.1f}" r="5" fill="#005eb8"/>')
        si26.append(f'<text x="{_lx26 - 42:.1f}" y="{_lyAvg - 10:.1f}" fill="#005eb8" font-size="12" font-weight="bold">{avg_price_pts[-1]:.2f}元</text>')
        si26.append(f'<circle cx="{_lx26:.1f}" cy="{_lyRt:.1f}" r="5" fill="#28a745"/>')
        si26.append(f'<text x="{_lx26 - 42:.1f}" y="{_lyRt - 10:.1f}" fill="#28a745" font-size="12" font-weight="bold">{ratios_all26[-1]:.3f}%</text>')

        # 十字矄准线元素（全局隐藏，JS交互时显示）
        si26.append(f'<g id="macro26Crosshair" style="display:none;">')
        si26.append(f'<line id="macro26CrossLine" y1="{padT26}" y2="{h - padB26}" stroke="#666" stroke-width="1.2" stroke-dasharray="3,3"/>')
        si26.append('<circle id="macro26HoverPt" r="5" fill="#005eb8" stroke="#fff" stroke-width="2"/>')
        si26.append('<circle id="macro26HoverRt" r="5" fill="#28a745" stroke="#fff" stroke-width="2"/>')
        si26.append('</g>')

        new_svg26 = f'<svg id="macro26Svg" viewBox="0 0 {w} {h}" style="width:100%;height:{h}px;display:block;">' + ''.join(si26) + '</svg>'
        html = re.sub(r'<svg id="macro26Svg"[^>]*>.*?</svg>', new_svg26, html, flags=re.DOTALL)

        badge26_html = f'<div id="macro26-badge" style="background:#f0fff4;color:#28a745;padding:6px 16px;border-radius:20px;font-weight:800;font-size:14px;border:1px solid #c3e6cb;">\n        当前比重: {ratios_all26[-1]:.3f}% (分散持仓全口径)\n      </div>'
        html = re.sub(r'<div id="macro26-badge"[^>]*>.*?</div>', badge26_html, html, flags=re.DOTALL)

        # 更新左轴说明文字
        html = html.replace(
            '左轴：中证全指(000985) 收盘点位 · 右轴(绿线)：26只ETF总市値占全A流通市値',
            'A股平均股价(来源:乐咕乐股) · 右轴(绿线)：26只ETF总市値占全A流通市値'
        )
    # ---- 第二图表结束 ----

    atomic_write_text(HTML_PATH, html)
    return html

def main():
    now = datetime.datetime.now()
    now_str = now.strftime('%Y-%m-%d %H:%M:%S')
    today_str = now.strftime('%Y%m%d')
    today_dash = now.strftime('%Y-%m-%d')
    
    print(f"[{now_str}] 🚀 开始执行国家队 ETF 自动化更新管线 (终极硬化版)...")
    
    if not os.path.exists(SNAPSHOT_PATH):
        print(f"[ERROR] 未找到快照文件: {SNAPSHOT_PATH}")
        sys.exit(1)
        
    with open(SNAPSHOT_PATH, 'r', encoding='utf-8') as f:
        content = f.read()

    prefix = "window.SNAPSHOT = "
    snapshot = json.loads(content.split(prefix)[1].rstrip().rstrip(';'))
    items = snapshot.get('items', [])
    tcodes = [it['tcode'] for it in items if 'tcode' in it]
    
    quotes = fetch_quotes(tcodes)
    if not quotes:
        print("[ERROR] 行情拉取为空，熔断退出。")
        sys.exit(1)
        
    total_today_amount = 0.0
    total_avg20_amount = 0.0
    
    for it in items:
        tc = it.get('tcode')
        q = quotes.get(tc)
        if q:
            it['price'] = q['price']
            it['pct'] = q['pct']
            it['prev_close'] = q['prev_close']
            it['amount_yi'] = round(q['amount_yi'], 4)
            it['amount_wan'] = q['amount_wan']
            it['outer_lots'] = q['outer_lots']
            it['inner_lots'] = q['inner_lots']
            it['turnover'] = q['turnover']
            it['mktcap_yi'] = round(q['mktcap_yi'], 2)
            # 腾讯第72字段返回总份数（单位：份），转换为亿份
            if q.get('shares', 0) > 0:
                new_shares_yi = round(q['shares'] / 1e8, 4)
                old_shares_yi = it.get('shares_yi', 0.0)
                # 检测腾讯份额字段是否停更（变化 < 0.1%）
                if old_shares_yi > 0 and abs(new_shares_yi - old_shares_yi) < old_shares_yi * 0.001:
                    # 腾讯停更，用换手率法 Fallback：份额 = 成交额(亿) / 换手率 / 当日清算单位净值
                    if q.get('turnover', 0) > 0 and q.get('amount_yi', 0) > 0:
                        nav_fallback = it.get('nav', q.get('price', new_shares_yi))  # 優先用清算单位净值
                        price = q.get('price', 0)
                        if price > 0 and q['turnover'] > 0:
                            scale_yi = q['amount_yi'] / (q['turnover'] / 100.0)
                            fb_shares = round(scale_yi / price, 4)
                            if fb_shares > old_shares_yi * 0.5:  # 合理性检查
                                it['shares_yi'] = fb_shares
                                print(f"  [Fallback] {it.get('code')} 腾讯停更，用换手率推算份额: {old_shares_yi}亿→{fb_shares}亿")
                    # 如果 fallback 也失败则保留旧值
                else:
                    it['shares_yi'] = new_shares_yi
            it['ts'] = q['ts']
            it['date'] = today_str
            
            total_today_amount += q['amount_yi']
            
            hist = it.get('hist', [])
            if hist and hist[-1].get('date') == today_dash:
                hist[-1]['amount_yi'] = round(q['amount_yi'], 4)
                hist[-1]['volume_lots'] = q['volume_lots']
            else:
                hist.append({
                    'date': today_dash,
                    'amount_yi': round(q['amount_yi'], 4),
                    'volume_lots': q['volume_lots']
                })
                if len(hist) > 30:
                    hist.pop(0)
            it['hist'] = hist
            
            last_20 = [h['amount_yi'] for h in hist[-20:]]
            if last_20:
                new_avg20 = sum(last_20) / float(len(last_20))
                it['avg20_yi'] = round(new_avg20, 2)
                total_avg20_amount += new_avg20

    
    # 自动将今日全部标的的最新份额记录追加到 shares_history（core 20 只 + 观察池，供宽基矩阵按大类聚合）
    sh_hist = snapshot.get('shares_history', {})
    core_day_shares = {}
    watch_day_shares = {}
    for it in items:
        c = it['code']
        s_yi = it.get('shares_yi', 0.0)
        if s_yi > 0:
            if it.get('group') == 'core':
                core_day_shares[c] = round(s_yi, 4)
            else:
                watch_day_shares[c] = round(s_yi, 4)
    # 份额延续：个别标的当日抓取失败（shares_yi<=0）时沿用上一交易日份额，
    # 避免 shares_history 序列出现断档——断档会让宽基净流入矩阵按「同日可比」规则
    # 丢掉该标的，表现为某宽基列的当日值凭空变成 null
    prev_dates = sorted([d for d in sh_hist.keys() if d < today_str])
    if prev_dates:
        prev_shares = sh_hist.get(prev_dates[-1]) or {}
        carried = 0
        for it in items:
            c = it['code']
            if c in core_day_shares or c in watch_day_shares:
                continue
            if prev_shares.get(c):
                if it.get('group') == 'core':
                    core_day_shares[c] = prev_shares[c]
                else:
                    watch_day_shares[c] = prev_shares[c]
                carried += 1
        if carried:
            print(f"ℹ️ 份额延续: {carried} 只标的当日无新抓取份额，按上一交易日 ({prev_dates[-1]}) 值延续")
    if core_day_shares:
        sh_hist[today_str] = core_day_shares
    if watch_day_shares:
        merged = dict(sh_hist.get(today_str) or {})
        merged.update(watch_day_shares)
        sh_hist[today_str] = merged
    if core_day_shares:
        snapshot['shares_history'] = sh_hist
        print(f"✅ shares_history 自动追加今日 ({today_str}) 最新份额 (核心 {len(core_day_shares)} 只 + 观察 {len(watch_day_shares)} 只)！")

    # 重算每只标的的当日日内成交额分布（items[].shape），修复陈旧样本与观察池标的缺失
    refresh_item_shapes(items)

    snapshot['trade_date'] = today_str
    snapshot['generated_at'] = now_str

    atomic_write_js(SNAPSHOT_PATH, 'SNAPSHOT', snapshot)

    # 生成大类宽基逐日净流入矩阵（独立数据文件，供前端矩阵表使用）
    try:
        build_widebase_netflow(items, sh_hist, today_str)
    except Exception as e:
        print(f"[ERROR] 宽基净流入矩阵生成失败: {e}")

    # 2. 更新宏观大底（含中证全指 + 26只ETF占比）
    with open(MACRO_PATH, 'r', encoding='utf-8') as f:
        macro = json.load(f)

    hs300_close = fetch_hs300_close()
    avg_price = fetch_avg_price()
    ratio_26 = calc_all26_ratio(items)

    if hs300_close:
        dates = macro['dates']
        pts = macro['hs300_points']
        ratios = macro['ratios']
        approx_ratio = round(0.783, 3)

        # 确保扩展字段存在（avg_price_points 替换原 zs985_points）
        avg_price_pts = macro.setdefault('avg_price_points', [0.0] * len(dates))
        ratios_all26 = macro.setdefault('ratios_all26', [0.0] * len(dates))

        if today_dash not in dates:
            dates.append(today_dash)
            pts.append(hs300_close)
            ratios.append(approx_ratio)
            avg_price_pts.append(round(avg_price, 2) if avg_price else (avg_price_pts[-1] if avg_price_pts else 0.0))
            ratios_all26.append(ratio_26)
        else:
            idx = dates.index(today_dash)
            pts[idx] = hs300_close
            ratios[idx] = approx_ratio
            if idx < len(avg_price_pts):
                avg_price_pts[idx] = round(avg_price, 2) if avg_price else avg_price_pts[idx]
                ratios_all26[idx] = ratio_26
            else:
                # 长度对齐（防止旧数据字段短）
                while len(avg_price_pts) < len(dates):
                    avg_price_pts.append(avg_price_pts[-1] if avg_price_pts else 0.0)
                    ratios_all26.append(ratio_26)
                avg_price_pts[idx] = round(avg_price, 2) if avg_price else avg_price_pts[idx]
                ratios_all26[idx] = ratio_26

        macro['avg_price_points'] = avg_price_pts
        macro['ratios_all26'] = ratios_all26
        macro['count'] = len(dates)
        atomic_write_json(MACRO_PATH, macro)
        print(f"✅ 宏观大底更新: 沪深300={hs300_close} A股平均股价={avg_price} 26只ETF占比={ratio_26}%")

    # 3. 物理抓取并生成分时双口径数据
    print("⏳ 正在采集与核算今日上证指数与 24 只 ETF 分时数据...")
    intraday_data = compute_and_save_intraday_min(items, today_dash)
    
    # 4. 重新渲染 index.html
    rebuild_index_html(macro, intraday_data, today_dash)
    print("✅ index.html 全局重新编译完成！")

    # 5. Git 提交与推送
    try:
        env = apply_proxy(os.environ.copy())
        env['PATH'] = f"/opt/homebrew/bin:/usr/local/bin:{env.get('PATH', '')}"
        
        subprocess.run(['git', 'add', 'data/', 'index.html'], cwd=REPO_DIR, check=True)
        # 只检查真正纳入版本管理的产物是否有变更，避免 logs/ 等未跟踪目录导致误判
        status_res = subprocess.run(['git', 'status', '--porcelain', '--', 'data', 'index.html'],
                                    cwd=REPO_DIR, capture_output=True, text=True)
        staged_res = subprocess.run(['git', 'diff', '--cached', '--name-only'],
                                    cwd=REPO_DIR, capture_output=True, text=True)
        has_changes = bool(status_res.stdout.strip()) or bool(staged_res.stdout.strip())
        if has_changes:
            msg = f"auto: daily EOD update {today_dash} with dual-scope intraday minute and macro data"
            subprocess.run(['git', 'commit', '-m', msg], cwd=REPO_DIR, check=True)
            subprocess.run(['git', 'push', 'origin', 'main'], cwd=REPO_DIR, env=env, check=True)
            print("🚀 GitHub Pages 远程代码库推送成功！")
        else:
            print("ℹ️ data/ 与 index.html 无实际变更，跳过提交。")
    except Exception as e:
        print(f"[ERROR] Git 提交/推送失败: {e}")

if __name__ == '__main__':
    main()
