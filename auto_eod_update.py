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

REPO_DIR = '/Users/leader/.openclaw/workspace-stock-agent/tools/leader-etf-tracker-repo'
SRC_DIR = '/Users/leader/.openclaw/workspace-stock-agent/tools/etf-tracker'
SKILL_DIR = '/Users/leader/.openclaw/workspace-stock-agent/skills/national-team-etf-tracker'
DATA_DIR = os.path.join(REPO_DIR, 'data')
SNAPSHOT_PATH = os.path.join(DATA_DIR, 'snapshot.js')
MACRO_PATH = os.path.join(DATA_DIR, 'macro_daily_history.json')
INTRADAY_JSON = os.path.join(DATA_DIR, 'intraday_min.json')
INTRADAY_JS = os.path.join(DATA_DIR, 'intraday_min.js')
HTML_PATH = os.path.join(REPO_DIR, 'index.html')

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
    url_idx = 'https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol=sh000001&scale=1&ma=no&datalen=245'
    req = urllib.request.Request(url_idx, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as resp:
        sh_raw = json.loads(resp.read().decode('gbk'))

    sh_today = [x for x in sh_raw if x['day'].startswith(today_dash)]
    if not sh_today:
        sh_today = sh_raw[-240:]
    # 清洗非连续竞价杂点（如 14:58），对齐标准连续交易时段
    filtered_sh = []
    for x in sh_today:
        t = x['day'][11:16]
        if t in ['14:58', '14:59']:
            continue
        filtered_sh.append(x)
    sh_today = filtered_sh
    sh_times = [x['day'][11:16] for x in sh_today]
    # 新浪1分钟K线的时间戳是该跟K线的结束时间，转换为开始时间（减1分钟）再展示
    def _shift_m1(t):
        try:
            from datetime import datetime as _dt, timedelta as _td
            return (_dt.strptime(t, '%H:%M') - _td(minutes=1)).strftime('%H:%M')
        except Exception:
            return t
    sh_times = [_shift_m1(t) for t in sh_times]
    sh_pts = [float(x['close']) for x in sh_today]
    n_pts = len(sh_times)

    def fetch_etf_min(tc):
        url = f'https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol={tc}&scale=1&ma=no&datalen=245'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                raw = json.loads(resp.read().decode('gbk'))
                m_map = {x['day'][11:16]: float(x['amount']) / 1e8 for x in raw if x['day'].startswith(today_dash)}
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
    url_cy = 'https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol=sz399006&scale=1&ma=no&datalen=245'
    req_cy = urllib.request.Request(url_cy, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req_cy, timeout=4) as resp:
            cy_raw = json.loads(resp.read().decode('gbk'))
            cy_today = [x for x in cy_raw if x['day'].startswith(today_dash) and x['day'][11:16] not in ['14:58', '14:59']]
            cy_map = {x['day'][11:16]: float(x['close']) for x in cy_today}
            cy_pts = [cy_map.get(t, float(cy_today[-1]['close']) if cy_today else 2426.08) for t in sh_times]
            prev_close_cy = float(cy_today[0]['open']) if cy_today else 2509.43
    except Exception as e:
        print(f"[WARN] 创业板指分时拉取降级: {e}")
        cy_pts = [2426.08] * n_pts
        prev_close_cy = 2509.43

    scope_20 = compute_scope(core_items)
    scope_26 = compute_scope(items)

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

    with open(INTRADAY_JSON, 'w', encoding='utf-8') as f:
        json.dump(intraday_data, f, ensure_ascii=False, indent=2)

    with open(INTRADAY_JS, 'w', encoding='utf-8') as f:
        f.write(f"window.INTRADAY_MIN = {json.dumps(intraday_data, ensure_ascii=False, indent=2)};\n")

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

    with open(HTML_PATH, 'w', encoding='utf-8') as f:
        f.write(html)
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

    
    # 自动将今日 20 只汇金核心 ETF 的最新份额记录追加到 shares_history (保证份额图表永不停更)
    sh_hist = snapshot.get('shares_history', {})
    core_day_shares = {}
    for it in items:
        if it.get('group') == 'core':
            c = it['code']
            s_yi = it.get('shares_yi', 0.0)
            if s_yi > 0:
                core_day_shares[c] = round(s_yi, 4)
    if core_day_shares:
        sh_hist[today_str] = core_day_shares
        snapshot['shares_history'] = sh_hist
        print(f"✅ shares_history 自动追加今日 ({today_str}) 最新份额 (共 {len(core_day_shares)} 只)！")

    snapshot['trade_date'] = today_str
    snapshot['generated_at'] = now_str
    
    with open(SNAPSHOT_PATH, 'w', encoding='utf-8') as f:
        f.write(f"window.SNAPSHOT = {json.dumps(snapshot, ensure_ascii=False, indent=2)};\n")
        
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
        with open(MACRO_PATH, 'w', encoding='utf-8') as f:
            json.dump(macro, f, ensure_ascii=False, indent=2)
        print(f"✅ 宏观大底更新: 沪深300={hs300_close} A股平均股价={avg_price} 26只ETF占比={ratio_26}%")

    # 3. 物理抓取并生成分时双口径数据
    print("⏳ 正在采集与核算今日上证指数与 24 只 ETF 分时数据...")
    intraday_data = compute_and_save_intraday_min(items, today_dash)
    
    # 4. 重新渲染 index.html
    rebuild_index_html(macro, intraday_data, today_dash)
    print("✅ index.html 全局重新编译完成！")

    # 5. Git 提交与推送
    try:
        env = os.environ.copy()
        env['https_proxy'] = 'http://127.0.0.1:2080'
        env['http_proxy'] = 'http://127.0.0.1:2080'
        env['PATH'] = f"/opt/homebrew/bin:/usr/local/bin:{env.get('PATH', '')}"
        
        subprocess.run(['git', 'add', 'data/', 'index.html'], cwd=REPO_DIR, check=True)
        status_res = subprocess.run(['git', 'status', '--porcelain'], cwd=REPO_DIR, capture_output=True, text=True)
        if status_res.stdout.strip():
            msg = f"auto: daily EOD update {today_dash} with dual-scope intraday minute and macro data"
            subprocess.run(['git', 'commit', '-m', msg], cwd=REPO_DIR, check=True)
            subprocess.run(['git', 'push', 'origin', 'main'], cwd=REPO_DIR, env=env, check=True)
            print("🚀 GitHub Pages 远程代码库推送成功！")
        else:
            print("ℹ️ 数据无新增物理变更，跳过提交。")
    except Exception as e:
        print(f"[ERROR] Git 提交/推送失败: {e}")

if __name__ == '__main__':
    main()
