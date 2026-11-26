#!/usr/bin/env python3
"""
分时历史回填脚本 (backfill_intraday_history.py)
功能：用新浪 1 分钟 K 线（datalen=1900，约可覆盖最近 8 个交易日）回填
      data/intraday_history.json 里缺失的最近 N 个交易日（默认 7 日）分时快照，
      供前端「盘中共振图」日期下拉回溯使用。
口径与 intraday_live_update.py 完全一致：
  - 时间槽规整 _norm_slot：<=14:57 前移 1 分钟；14:58/14:59/15:00 聚合为 '15:00'
  - 同一时间槽多根 K 线成交额累加
  - base_cum_amount = 当日 20 日均额合计（快照 avg20_yi）× 均匀分布，与实时口径一致
用法：
  python3 backfill_intraday_history.py            # 回填最近 7 个交易日（不覆盖已有日期）
  python3 backfill_intraday_history.py --days 7 --force
"""
import os, sys, json, time, urllib.request
from datetime import datetime, timedelta

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(REPO_DIR, 'data')
INTRADAY_HIST_JSON = os.path.join(DATA_DIR, 'intraday_history.json')
INTRADAY_HIST_JS = os.path.join(DATA_DIR, 'intraday_history.js')
SNAPSHOT_PATH = os.path.join(DATA_DIR, 'snapshot.js')

KEEP_DAYS = 20
CLOSE_SLOT = '15:00'
DATALEN = 1900

HEADERS = {'User-Agent': 'Mozilla/5.0'}


# ---------- 原子写盘（与盘中/EOD 脚本同一序列化口径） ----------
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


# ---------- 时间槽规整：与 intraday_live_update._norm_slot 保持一致 ----------
def _shift_m1(t):
    try:
        return (datetime.strptime(t, '%H:%M') - timedelta(minutes=1)).strftime('%H:%M')
    except Exception:
        return t


def _norm_slot(t):
    if t <= '14:57':
        return _shift_m1(t)
    if t in ('14:58', '14:59', '15:00'):
        return CLOSE_SLOT
    return t


# ---------- 数据抓取 ----------
def fetch_min_kline(symbol, datalen=DATALEN, retry=2):
    url = ('https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData'
           f'?symbol={symbol}&scale=1&ma=no&datalen={datalen}')
    last_err = None
    for _ in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode('gbk'))
            if data:
                return data
            last_err = RuntimeError('empty response')
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(1.2)
    raise RuntimeError(f'fetch {symbol} failed: {last_err}')


def fetch_daily_close(symbol, datalen=30):
    """日 K（scale=240），返回 [(date_str, close), ...] 升序"""
    url = ('https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData'
           f'?symbol={symbol}&scale=240&ma=no&datalen={datalen}')
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode('gbk'))
    out = []
    for x in data or []:
        try:
            out.append((x['day'][:10], float(x['close'])))
        except Exception:  # noqa: BLE001
            continue
    return out


def load_snapshot():
    with open(SNAPSHOT_PATH, 'r', encoding='utf-8') as f:
        s = f.read()
    i = s.index('{')
    return json.loads(s[i:].rstrip().rstrip(';'))


def group_bars_by_day(rows):
    """把 1 分钟 K 线按交易日分组，并按时间槽去重（保留最后一条）"""
    days = {}
    for x in rows:
        d = x['day'][:10]
        t_raw = x['day'][11:16]
        if t_raw > '15:00':
            continue
        days.setdefault(d, {})[_norm_slot(t_raw)] = x
    return days


def build_axis(index_day_slots):
    """以指数时间槽为轴，返回有序 times 列表（含 15:00）"""
    return sorted(index_day_slots.keys(), key=lambda t: _slot_key(t))


def _slot_key(t):
    h, m = t.split(':')
    return int(h) * 60 + int(m)


def build_day(date_dash, sh_slots, cy_slots, etf_day_by_code, snapshot):
    times = build_axis(sh_slots)
    if not times:
        return None
    sh_pts = []
    cy_pts = []
    last_sh = None
    last_cy = None
    for t in times:
        b = sh_slots.get(t)
        if b and b.get('close'):
            last_sh = float(b['close'])
        sh_pts.append(round(last_sh, 3) if last_sh is not None else None)
        b2 = cy_slots.get(t)
        if b2 and b2.get('close'):
            last_cy = float(b2['close'])
        cy_pts.append(round(last_cy, 3) if last_cy is not None else None)

    def scope(scope_items):
        n = len(times)
        min_amt = []
        for t in times:
            v = 0.0
            for it in scope_items:
                code = it.get('tcode') if isinstance(it, dict) else it
                bar = etf_day_by_code.get(code, {}).get(t)
                if bar:
                    try:
                        v += float(bar.get('amount') or 0) / 1e8
                    except Exception:  # noqa: BLE001
                        pass
            min_amt.append(round(v, 2))
        cum, cur = [], 0.0
        for v in min_amt:
            cur += v
            cum.append(round(cur, 2))
        tot_today = cum[-1] if cum else 0.0
        tot_avg20 = round(sum(it.get('avg20_yi', 0) or 0 for it in scope_items), 2)
        base_cum = [round(tot_avg20 * ((i + 1) / float(n)), 2) for i in range(n)]
        peak_val = max(min_amt) if min_amt else 0.0
        peak_i = min_amt.index(peak_val) if min_amt else 0
        return {
            'count': len(scope_items),
            'total_today_amount': tot_today,
            'total_avg20_amount': tot_avg20,
            'ratio': round(tot_today / tot_avg20, 2) if tot_avg20 > 0 else 1.0,
            'peak_time': times[peak_i] if times else '09:30',
            'peak_amount': peak_val,
            'min_amount': min_amt,
            'cum_amount': cum,
            'base_cum_amount': base_cum,
        }

    items = snapshot.get('items', [])
    core_items = [it for it in items if it.get('group') == 'core']

    return {
        'date': date_dash,
        'prev_close': None,   # 由调用方填充
        'times': times,
        'sh_points': sh_pts,
        'cy_points': cy_pts,
        'prev_close_cy': None,
        'scope_20': scope(core_items),
        'scope_26': scope(items),
        'source': 'sina-1m-backfill',
    }


def main():
    args = sys.argv[1:]
    want_days = 7
    force = '--force' in args
    if '--days' in args:
        try:
            want_days = int(args[args.index('--days') + 1])
        except Exception:  # noqa: BLE001
            want_days = 7

    snapshot = load_snapshot()
    items = snapshot.get('items', [])
    etf_codes = [it['tcode'] for it in items if it.get('tcode')]

    print(f'[INFO] ETF 数量 {len(etf_codes)}，目标回填 {want_days} 个交易日')

    print('[INFO] 拉取上证指数/创业板指 1 分钟线...')
    sh_rows = fetch_min_kline('sh000001')
    time.sleep(0.2)
    cy_rows = fetch_min_kline('sz399006')

    sh_by_day = group_bars_by_day(sh_rows)
    cy_by_day = group_bars_by_day(cy_rows)
    trade_days = sorted([d for d, s in sh_by_day.items() if len(s) >= 200])
    print(f'[INFO] 指数可用交易日: {trade_days}')
    target_days = trade_days[-want_days:]

    print('[INFO] 拉取 ETF 1 分钟线（26 只）...')
    etf_rows = {}
    for idx, code in enumerate(etf_codes, 1):
        try:
            etf_rows[code] = fetch_min_kline(code)
            print(f'  [{idx}/{len(etf_codes)}] {code} ok')
        except Exception as e:  # noqa: BLE001
            print(f'  [{idx}/{len(etf_codes)}] {code} FAIL {e}')
        time.sleep(0.15)

    etf_by_day = {code: group_bars_by_day(rows) for code, rows in etf_rows.items()}

    # 前收盘（用于涨跌幅轴）
    sh_daily = fetch_daily_close('sh000001')
    cy_daily = fetch_daily_close('sz399006')
    sh_close_map = dict(sh_daily)
    cy_close_map = dict(cy_daily)
    all_daily_days = sorted(sh_close_map.keys())

    def prev_close_of(date_dash, close_map, fallback):
        prevs = [d for d in all_daily_days if d < date_dash]
        if prevs:
            return close_map.get(prevs[-1], fallback)
        return fallback

    # 读取既有归档
    hist = {}
    if os.path.exists(INTRADAY_HIST_JSON):
        try:
            with open(INTRADAY_HIST_JSON, 'r', encoding='utf-8') as f:
                hist = json.load(f) or {}
        except Exception as e:  # noqa: BLE001
            print(f'[WARN] 归档读取失败，将重建: {e}')
            hist = {}
    days = hist.get('days') or {}

    added, skipped = [], []
    for d in target_days:
        if d in days and not force:
            skipped.append(d)
            continue
        etf_day_by_code = {}
        for code in etf_codes:
            etf_day_by_code[code] = etf_by_day.get(code, {}).get(d, {})
        day_obj = build_day(d, sh_by_day[d], cy_by_day.get(d, {}), etf_day_by_code, snapshot)
        if not day_obj:
            print(f'  [WARN] {d} 无有效时点，跳过')
            continue
        day_obj['prev_close'] = round(prev_close_of(d, sh_close_map, day_obj['sh_points'][0] or 0), 2)
        day_obj['prev_close_cy'] = round(prev_close_of(d, cy_close_map, (day_obj['cy_points'][0] or 0)), 2)
        days[d] = day_obj
        added.append(d)
        print(f'  [OK] {d} times={len(day_obj["times"])} 末位={day_obj["times"][-1]} '
              f'20只当日额={day_obj["scope_20"]["total_today_amount"]}亿')

    ordered = sorted(days.keys())[-KEEP_DAYS:]
    payload = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'keep_days': KEEP_DAYS,
        'dates': ordered,
        'days': {d: days[d] for d in ordered},
        'notes': ('按交易日归档的盘中分时快照，滚动保留最近 keep_days 个交易日，供前端日期回溯与多日对比。'
                  '实时快照由 intraday_live_update.py 写入；source=sina-1m-backfill 的为历史回填（新浪 1 分钟线重算，'
                  'base_cum_amount 仍按快照 20 日均额合计均摊）。'),
    }
    atomic_write_json(INTRADAY_HIST_JSON, payload)
    atomic_write_js(INTRADAY_HIST_JS, 'INTRADAY_HISTORY', payload)
    print(f'[DONE] 新增 {len(added)} 天 {added}；已有跳过 {len(skipped)} 天 {skipped}')
    print(f'[DONE] dates 共 {len(ordered)} 个: {ordered}')


if __name__ == '__main__':
    main()
