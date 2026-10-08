# -*- coding: utf-8 -*-
"""
双色球 / 大乐透 历史数据分析与号码预测工具
================================================
- 自动联网抓取官方历史开奖数据（中国福彩网 cwl.gov.cn、中国体彩网 sporttery.cn）
- 采用遗漏值、和值、跨度、奇偶比、大小比、热冷号、断区、轮动等分析方法
- 基于加权评分模型给出下一期参考号码
- 本地起一个小型 HTTP 服务，把分析结果渲染成网页，浏览器自动打开

【郑重声明】彩票开奖为独立随机事件，本工具仅做统计规律展示与分析参考，
不构成任何中奖保证，请理性投注。
"""

import http.server
import socketserver
import json
import urllib.request
import urllib.parse
import threading
import webbrowser
import os
import re
import socket
import sys
import time
from collections import Counter

# ------------------------------------------------------------------
# 1. 数据抓取
# ------------------------------------------------------------------
SSQ_API = (
    "https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice"
    "?name=ssq&issueCount={n}&pageNo=1&pageSize={n}&systemType=PC"
)
DLT_API = (
    "https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry"
    "?gameNo=85&provinceId=0&pageSize={n}&isVerify=1&pageNo=1"
)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _fetch_json(url, referer):
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Referer": referer,
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", "ignore")
    return json.loads(raw)


def fetch_ssq(count=100):
    """双色球：每期 6 个红球(1-33) + 1 个蓝球(1-16)。接口返回最新在前。"""
    data = _fetch_json(SSQ_API.format(n=count), "https://www.cwl.gov.cn/")
    rows = []
    for it in data.get("result") or []:
        red = [int(x) for x in it["red"].split(",") if x.strip()]
        blue = int(it["blue"])
        if len(red) != 6:
            continue
        rows.append({
            "issue": it["code"],
            "date": re.sub(r"\(.*?\)", "", it.get("date", "")).strip(),
            "week": it.get("week", ""),
            "red": red,
            "blue": blue,
            "red_str": ",".join("%02d" % x for x in red),
            "blue_str": "%02d" % blue,
        })
    # 按期号升序（旧 -> 新）
    rows.sort(key=lambda r: r["issue"])
    return rows


def fetch_dlt(count=100):
    """大乐透：每期 5 个前区(1-35) + 2 个后区(1-12)。接口返回最新在前。"""
    data = _fetch_json(DLT_API.format(n=count), "https://www.sporttery.cn/")
    lst = (data.get("value") or {}).get("list") or []
    rows = []
    for it in lst:
        res = (it.get("lotteryDrawResult") or "").split()
        nums = [int(x) for x in res if x.strip()]
        if len(nums) != 7:
            continue
        fronts, backs = nums[:5], nums[5:]
        rows.append({
            "issue": it.get("lotteryDrawNum"),
            "date": (it.get("lotteryDrawTime") or "")[:10],
            "fronts": fronts,
            "backs": backs,
            "front_str": " ".join("%02d" % x for x in fronts),
            "back_str": " ".join("%02d" % x for x in backs),
        })
    rows.sort(key=lambda r: r["issue"])
    return rows


# ------------------------------------------------------------------
# 2. 分析方法
# ------------------------------------------------------------------
def compute_omission(values_by_issue, pool):
    """遗漏值：每个号码距离上一次开出的期数（当前仍未开出则累计至今）。"""
    pool = list(range(pool[0], pool[1] + 1))
    n = len(values_by_issue)          # 期数（最新在最后）
    last = {num: None for num in pool}
    for idx, nums in enumerate(values_by_issue):
        for num in nums:
            last[num] = idx
    omission = {}
    for num in pool:
        li = last[num]
        omission[num] = (n - 1 - li) if li is not None else n
    return omission


def compute_frequency(values_by_issue, pool, window=50):
    """热冷号：统计近 window 期每个号码出现次数。"""
    pool = list(range(pool[0], pool[1] + 1))
    cnt = Counter()
    for nums in values_by_issue[-window:]:
        for num in nums:
            cnt[num] += 1
    freq = {num: cnt.get(num, 0) for num in pool}
    return freq


def compute_rotation(values_by_issue, pool, win_a=15, win_b=40):
    """轮动/趋势：近期出现率相对中期出现率的增减（近期更热为正）。"""
    pool = list(range(pool[0], pool[1] + 1))
    recent = compute_frequency(values_by_issue, pool, win_a)
    medium = compute_frequency(values_by_issue, pool, win_b)
    rotation = {}
    for num in pool:
        r = recent.get(num, 0) / win_a
        m = medium.get(num, 0) / win_b
        rotation[num] = round(r - m, 4)
    return rotation


def sums(values_by_issue):
    return [sum(v) for v in values_by_issue]


def spans(values_by_issue):
    return [max(v) - min(v) for v in values_by_issue]


def parity_ratio(values_by_issue):
    return [(sum(1 for x in v if x % 2 == 1), sum(1 for x in v if x % 2 == 0)) for v in values_by_issue]


def bigsmall_ratio(values_by_issue, boundary):
    """大小比：<= boundary 记为小，> boundary 记为大。"""
    return [(sum(1 for x in v if x <= boundary), sum(1 for x in v if x > boundary)) for v in values_by_issue]


def zone_distribution(values_by_issue, zone_defs):
    """分区分布：zone_defs 为 (start, end) 列表，返回每期的各区命中数。"""
    out = []
    for v in values_by_issue:
        z = []
        for (a, b) in zone_defs:
            z.append(sum(1 for x in v if a <= x <= b))
        out.append(z)
    return out


def hotcold_class(freq, pool_range):
    """把频次分为 热 / 温 / 冷。"""
    pool = list(range(pool_range[0], pool_range[1] + 1))
    vals = sorted(freq[num] for num in pool)
    lo = vals[len(vals) // 3]
    hi = vals[2 * len(vals) // 3]
    cls = {}
    for num in pool:
        f = freq[num]
        if f >= hi:
            cls[num] = "热"
        elif f <= lo:
            cls[num] = "冷"
        else:
            cls[num] = "温"
    return cls


# --- 新增：AC值 / 012路 / 质合比 / 连号 / 重号 / 邻号 / 尾和 / 和值振幅 ---
_PRIMES = {2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31}


def ac_value(nums):
    """AC值（号码复杂度）：两两差值去重个数 - (号码数-1)。
    值越大说明号码分布越散、越复杂；是衡量一组号码结构离散度的常用权威指标。"""
    d = set()
    for i in range(len(nums)):
        for j in range(i + 1, len(nums)):
            d.add(abs(nums[i] - nums[j]))
    return len(d) - (len(nums) - 1)


def ac_values(values_by_issue):
    return [ac_value(v) for v in values_by_issue]


def mod3_counts(values_by_issue):
    """012路（除3余数）：每期号码按对3取余归为0/1/2路，返回各自个数。"""
    return [[sum(1 for x in v if x % 3 == r) for r in (0, 1, 2)] for v in values_by_issue]


def prime_composite(values_by_issue):
    """质合比：每期 (质数个数, 合数个数)。1 按合数口径计入。"""
    out = []
    for v in values_by_issue:
        p = sum(1 for x in v if x in _PRIMES)
        out.append((p, len(v) - p))
    return out


def consecutive_pairs(values_by_issue):
    """连号：每期连续号码对数量（如 05 06 计 1 对）。"""
    out = []
    for v in values_by_issue:
        s = set(v)
        out.append(sum(1 for x in v if (x + 1) in s))
    return out


def repeat_count(values_by_issue):
    """重号：每期与上期相同的号码个数（首期为 0）。"""
    out = [0]
    for i in range(1, len(values_by_issue)):
        prev = set(values_by_issue[i - 1])
        out.append(sum(1 for x in values_by_issue[i] if x in prev))
    return out


def adjacent_count(values_by_issue):
    """邻号：每期落在上期号码±1范围内的个数（首期为 0）。"""
    out = [0]
    for i in range(1, len(values_by_issue)):
        prev = set()
        for x in values_by_issue[i - 1]:
            prev.add(x - 1)
            prev.add(x + 1)
        out.append(sum(1 for x in values_by_issue[i] if x in prev))
    return out


def tail_sum(values_by_issue):
    """尾数和：每期所有号码个位数字之和。"""
    return [sum(x % 10 for x in v) for v in values_by_issue]


def sum_amplitude(sum_list):
    """和值振幅：相邻两期和值之差的绝对值（首期为 0）。"""
    out = [0]
    for i in range(1, len(sum_list)):
        out.append(abs(sum_list[i] - sum_list[i - 1]))
    return out


# ------------------------------------------------------------------
# 3. 预测模型（加权评分）
# ------------------------------------------------------------------
def _normalize(freq, pool_range):
    pool = list(range(pool_range[0], pool_range[1] + 1))
    vals = [freq.get(n, 0) for n in pool]
    mn, mx = min(vals), max(vals)
    if mx == mn:
        return {n: 0.0 for n in pool}
    return {n: (freq.get(n, 0) - mn) / (mx - mn) for n in pool}


def score_numbers(omission, freq, rotation, avg_omission, weight):
    """单号码评分（0-100）。"""
    pool = list(omission.keys())
    f_norm = _normalize(freq, (min(pool), max(pool)))
    r_max = max((abs(rotation.get(n, 0)) for n in pool), default=1)
    score = {}
    for n in pool:
        om = omission[n]
        avg = avg_omission.get(n, 1)
        # 遗漏分：当遗漏接近平均遗漏时最“该出”；略超平均给出更高权重，极端超期下调（防冷陷阱）
        ratio = om / avg if avg else om
        if om == 0:
            o_score = 20.0          # 刚开出，追冷
        elif ratio < 0.5:
            o_score = 30.0 + 40.0 * ratio
        elif ratio < 1.5:
            o_score = 70.0 + 20.0 * (ratio - 0.5) / 1.0
        else:
            o_score = 90.0 - 30.0 * min(1.0, (ratio - 1.5) / 2.0)
        f_score = f_norm.get(n, 0.5) * 100.0
        r_score = (0.5 + (rotation.get(n, 0) / (2 * r_max if r_max else 1)) / 2) * 100.0
        score[n] = (
            weight["omission"] * o_score
            + weight["freq"] * f_score
            + weight["rotation"] * r_score
        ) / (weight["omission"] + weight["freq"] + weight["rotation"])
    return score


def pick_combos(scores, pool_range, zone_defs, boundary, center_sum, span_lo, span_hi,
                pick, parity_targets, max_zone_drop=1, max_combos=6,
                ac_range=(0, 99), prime_min=0):
    """基于约束 + 总分挑选若干组合。
    在原有和值/跨度/奇偶/断区约束之上，新增 AC 值(号码复杂度)区间 与 质数个数下限 约束，
    使推荐组合更贴合历史结构规律。
    scores: {num: 分数}; pick: 每注号码个数。"""
    pool = list(range(pool_range[0], pool_range[1] + 1))
    ranked = sorted(pool, key=lambda n: scores.get(n, 0), reverse=True)
    top = ranked[: max(14, pick * 3)]          # 从高分池组号
    ac_lo, ac_hi = ac_range

    def _build(base):
        s = sum(base)
        sp = max(base) - min(base)
        odd = sum(1 for x in base if x % 2 == 1)
        even = pick - odd
        zcnt = [sum(1 for x in base if a <= x <= b) for (a, b) in zone_defs]
        ac = ac_value(base)
        pc = sum(1 for x in base if x in _PRIMES)
        return {
            "nums": sorted(base), "sum": s, "span": sp,
            "odd_even": "%d:%d" % (odd, even),
            "zone": ":".join(map(str, zcnt)),
            "ac": ac, "prime": pc,
            "ac_str": str(ac),
            "prime_str": "%d:%d" % (pc, pick - pc),
            "zcnt": zcnt, "odd": odd, "even": even,
            "score": round(sum(scores[x] for x in base) / pick, 1),
        }

    def _pass(c, allow_zone, allow_parity, a_lo, a_hi, p_min):
        if not (center_sum - span_hi <= c["sum"] <= center_sum + span_hi):
            return False
        if c["span"] < span_lo:
            return False
        if sum(1 for z in c["zcnt"] if z == 0) > allow_zone:
            return False
        if not (a_lo <= c["ac"] <= a_hi):
            return False
        if c["prime"] < p_min:
            return False
        if not allow_parity:
            t = (c["odd"], c["even"])
            if t not in parity_targets and (t[1], t[0]) not in parity_targets:
                return False
        return True

    combos, seen = [], set()
    # 严格模式：滑动起点，收集满足全部约束的组合
    for start in range(0, len(top) - pick + 1):
        if len(combos) >= max_combos:
            break
        base = top[start:start + pick]
        c = _build(base)
        if not _pass(c, max_zone_drop, allow_parity=False, a_lo=ac_lo, a_hi=ac_hi, p_min=prime_min):
            continue
        key = tuple(sorted(base))
        if key in seen:
            continue
        seen.add(key)
        combos.append(c)
    # 若严格模式产出太少，放宽：允许一个断区 + 奇偶略偏离 + AC/质数约束略微放宽
    if len(combos) < 3:
        relaxed = []
        for start in range(0, len(top) - pick + 1):
            if len(combos) + len(relaxed) >= max_combos:
                break
            base = top[start:start + pick]
            c = _build(base)
            if _pass(c, max_zone_drop + 1, allow_parity=True,
                    a_lo=ac_lo - 1, a_hi=ac_hi + 1, p_min=max(0, prime_min - 1)):
                key = tuple(sorted(base))
                if key in seen:
                    continue
                seen.add(key)
                relaxed.append(c)
        combos.extend(relaxed[: max(0, max_combos - len(combos))])
    return combos


def analyze_ssq(rows):
    reds = [r["red"] for r in rows]
    blues = [[r["blue"]] for r in rows]
    red_pool = (1, 33)
    blue_pool = (1, 16)
    zone_defs = [(1, 11), (12, 22), (23, 33)]
    boundary = 16          # 双色球大小分界

    red_om = compute_omission(reds, red_pool)
    blue_om = compute_omission(blues, blue_pool)
    red_freq = compute_frequency(reds, red_pool, 50)
    blue_freq = compute_frequency(blues, blue_pool, 50)
    red_rot = compute_rotation(reds, red_pool, 15, 40)
    blue_rot = compute_rotation(blues, blue_pool, 15, 40)

    # 平均遗漏（按出现频次推算）
    n = len(reds)
    red_avg = {num: (n / red_freq[num] if red_freq[num] else n + 5) for num in range(1, 34)}
    blue_avg = {num: (n / blue_freq[num] if blue_freq[num] else n + 5) for num in range(1, 17)}

    red_w = {"omission": 0.50, "freq": 0.30, "rotation": 0.20}
    blue_w = {"omission": 0.55, "freq": 0.25, "rotation": 0.20}
    red_score = score_numbers(red_om, red_freq, red_rot, red_avg, red_w)
    blue_score = score_numbers(blue_om, blue_freq, blue_rot, blue_avg, blue_w)

    sum_list = sums(reds)
    span_list = spans(reds)
    center_sum = round(sum(sum_list[-30:]) / 30) if len(sum_list) else 100
    span_mid = round(sum(span_list[-30:]) / 30) if len(span_list) else 25
    span_lo = max(8, span_mid - 8)
    span_hi = span_mid + 8

    parity_targets = [(3, 3), (2, 4), (4, 2)]
    # 新增：AC值 / 质数 约束（依据近期历史统计区间）
    _ac = ac_values(reds)
    ac_mid = round(sum(_ac[-30:]) / 30) if _ac else 5
    ac_lo = max(0, ac_mid - 1)
    ac_hi = ac_mid + 1
    _p = [p for p, _ in prime_composite(reds)]
    prime_min = max(0, round(sum(_p[-30:]) / 30) - 1) if _p else 1
    combos = pick_combos(red_score, red_pool, zone_defs, boundary, center_sum,
                         span_lo, span_hi, 6, parity_targets, max_zone_drop=1, max_combos=6,
                         ac_range=(ac_lo, ac_hi), prime_min=prime_min)
    # 蓝球：分数前 4
    blue_ranked = sorted(range(1, 17), key=lambda n: blue_score[n], reverse=True)

    return {
        "game": "双色球",
        "latest": rows[-1],
        "count": len(rows),
        "stats": {
            "sum": {"list": sum_list[-80:], "mean": round(sum(sum_list) / len(sum_list), 1),
                    "center": center_sum, "range": [min(sum_list), max(sum_list)]},
            "span": {"list": span_list[-80:], "mean": round(sum(span_list) / len(span_list), 1),
                     "range": [min(span_list), max(span_list)]},
            "parity": {"list": parity_ratio(reds)[-80:]},
            "bigsmall": {"list": bigsmall_ratio(reds, boundary)[-80:],
                         "boundary": "小≤%d, 大≥%d" % (boundary, boundary + 1)},
            "zone": {"list": zone_distribution(reds, zone_defs)[-80:], "defs": ["1-11", "12-22", "23-33"]},
            "ac": {"list": ac_values(reds)[-80:], "mean": round(sum(ac_values(reds)) / len(reds), 1)},
            "mod3": {"list": mod3_counts(reds)[-80:], "defs": ["0路", "1路", "2路"]},
            "prime": {"list": prime_composite(reds)[-80:]},
            "consec": {"list": consecutive_pairs(reds)[-80:]},
            "repeat": {"list": repeat_count(reds)[-80:]},
            "adjacent": {"list": adjacent_count(reds)[-80:]},
            "tail": {"list": tail_sum(reds)[-80:]},
            "sum_amp": {"list": sum_amplitude(sum_list)[-80:]},
        },
        "analysis": {
            "omission": {"red": red_om, "blue": blue_om},
            "freq": {"red": red_freq, "blue": blue_freq,
                     "red_class": hotcold_class(red_freq, red_pool),
                     "blue_class": hotcold_class(blue_freq, blue_pool)},
            "rotation": {"red": red_rot, "blue": blue_rot},
            "hot_red": [n for n, c in hotcold_class(red_freq, red_pool).items() if c == "热"],
            "cold_red": [n for n, c in hotcold_class(red_freq, red_pool).items() if c == "冷"],
        },
        "prediction": {
            "red_score_top": sorted(red_score, key=red_score.get, reverse=True)[:12],
            "blue_score_top": blue_ranked[:4],
            "combos": combos,
            "blue_picks": blue_ranked[:4],
            "score_red": red_score,
            "score_blue": blue_score,
        },
    }


def analyze_dlt(rows):
    fronts = [r["fronts"] for r in rows]
    backs = [r["backs"] for r in rows]
    front_pool = (1, 35)
    back_pool = (1, 12)
    zone_defs = [(1, 12), (13, 24), (25, 35)]
    boundary = 17           # 大乐透前区分界

    f_om = compute_omission(fronts, front_pool)
    b_om = compute_omission(backs, back_pool)
    f_freq = compute_frequency(fronts, front_pool, 50)
    b_freq = compute_frequency(backs, back_pool, 50)
    f_rot = compute_rotation(fronts, front_pool, 15, 40)
    b_rot = compute_rotation(backs, back_pool, 15, 40)

    n = len(fronts)
    f_avg = {num: (n / f_freq[num] if f_freq[num] else n + 5) for num in range(1, 36)}
    b_avg = {num: (n / b_freq[num] if b_freq[num] else n + 5) for num in range(1, 13)}

    f_w = {"omission": 0.50, "freq": 0.30, "rotation": 0.20}
    b_w = {"omission": 0.55, "freq": 0.25, "rotation": 0.20}
    f_score = score_numbers(f_om, f_freq, f_rot, f_avg, f_w)
    b_score = score_numbers(b_om, b_freq, b_rot, b_avg, b_w)

    sum_list = sums(fronts)
    span_list = spans(fronts)
    center_sum = round(sum(sum_list[-30:]) / 30) if len(sum_list) else 90
    span_mid = round(sum(span_list[-30:]) / 30) if len(span_list) else 20
    span_lo = max(6, span_mid - 6)
    span_hi = span_mid + 6

    parity_targets = [(3, 2), (2, 3), (4, 1)]
    # 新增：AC值 / 质数 约束
    _ac = ac_values(fronts)
    ac_mid = round(sum(_ac[-30:]) / 30) if _ac else 5
    ac_lo = max(0, ac_mid - 1)
    ac_hi = ac_mid + 1
    _p = [p for p, _ in prime_composite(fronts)]
    prime_min = max(0, round(sum(_p[-30:]) / 30) - 1) if _p else 1
    combos = pick_combos(f_score, front_pool, zone_defs, boundary, center_sum,
                         span_lo, span_hi, 5, parity_targets, max_zone_drop=1, max_combos=6,
                         ac_range=(ac_lo, ac_hi), prime_min=prime_min)
    # 后区：两码，直接取前 6 高分做组合评分
    b_ranked = sorted(range(1, 13), key=lambda n: b_score[n], reverse=True)[:6]
    back_combos = []
    for i in range(len(b_ranked) - 1):
        if len(back_combos) >= 4:
            break
        a, b = b_ranked[i], b_ranked[i + 1]
        back_combos.append({"nums": sorted([a, b]),
                            "score": round((b_score[a] + b_score[b]) / 2, 1)})

    return {
        "game": "大乐透",
        "latest": rows[-1],
        "count": len(rows),
        "stats": {
            "sum": {"list": sum_list[-80:], "mean": round(sum(sum_list) / len(sum_list), 1),
                    "center": center_sum, "range": [min(sum_list), max(sum_list)]},
            "span": {"list": span_list[-80:], "mean": round(sum(span_list) / len(span_list), 1),
                     "range": [min(span_list), max(span_list)]},
            "parity": {"list": parity_ratio(fronts)[-80:]},
            "bigsmall": {"list": bigsmall_ratio(fronts, boundary)[-80:],
                         "boundary": "小≤%d, 大≥%d" % (boundary, boundary + 1)},
            "zone": {"list": zone_distribution(fronts, zone_defs)[-80:], "defs": ["1-12", "13-24", "25-35"]},
            "ac": {"list": ac_values(fronts)[-80:], "mean": round(sum(ac_values(fronts)) / len(fronts), 1)},
            "mod3": {"list": mod3_counts(fronts)[-80:], "defs": ["0路", "1路", "2路"]},
            "prime": {"list": prime_composite(fronts)[-80:]},
            "consec": {"list": consecutive_pairs(fronts)[-80:]},
            "repeat": {"list": repeat_count(fronts)[-80:]},
            "adjacent": {"list": adjacent_count(fronts)[-80:]},
            "tail": {"list": tail_sum(fronts)[-80:]},
            "sum_amp": {"list": sum_amplitude(sum_list)[-80:]},
        },
        "analysis": {
            "omission": {"front": f_om, "back": b_om},
            "freq": {"front": f_freq, "back": b_freq,
                     "front_class": hotcold_class(f_freq, front_pool),
                     "back_class": hotcold_class(b_freq, back_pool)},
            "rotation": {"front": f_rot, "back": b_rot},
            "hot_front": [n for n, c in hotcold_class(f_freq, front_pool).items() if c == "热"],
            "cold_front": [n for n, c in hotcold_class(f_freq, front_pool).items() if c == "冷"],
        },
        "prediction": {
            "front_score_top": sorted(f_score, key=f_score.get, reverse=True)[:10],
            "back_score_top": sorted(b_score, key=b_score.get, reverse=True)[:4],
            "combos": combos,
            "back_combos": back_combos,
            "score_front": f_score,
            "score_back": b_score,
        },
    }


# ------------------------------------------------------------------
# 4. HTTP 服务
# ------------------------------------------------------------------
CACHE = {}
CACHE_TIME = {}


def get_analysis(game):
    now = time.time()
    if game in CACHE and now - CACHE_TIME.get(game, 0) < 300:
        return CACHE[game]
    if game == "ssq":
        rows = fetch_ssq(100)
        result = analyze_ssq(rows)
    else:
        rows = fetch_dlt(100)
        result = analyze_dlt(rows)
    CACHE[game] = result
    CACHE_TIME[game] = now
    return result


def get_lan_ip():
    """获取本机在局域网内的 IPv4 地址（供手机在同一 WiFi 下访问）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and ip != "0.0.0.0":
            return ip
    except Exception:
        pass
    # 兜底：取第一个非回环 IPv4 地址
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                return ip
    except Exception:
        pass
    return "127.0.0.1"


if getattr(sys, "frozen", False):      # PyInstaller 打包后：页面文件在临时解压目录
    BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
else:                                   # 源码运行（bat）：页面在脚本同目录
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "index.html")


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                with open(HTML_PATH, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path == "/api/analysis":
                q = urllib.parse.parse_qs(parsed.query)
                game = q.get("game", ["ssq"])[0]
                if game not in ("ssq", "dlt"):
                    self._send(400, json.dumps({"error": "unknown game"}).encode())
                    return
                result = get_analysis(game)
                self._send(200, json.dumps(result, ensure_ascii=False).encode())
            elif path == "/api/refresh":
                game = urllib.parse.parse_qs(parsed.query).get("game", ["ssq"])[0]
                CACHE.pop(game, None)
                CACHE_TIME.pop(game, None)
                result = get_analysis(game)
                self._send(200, json.dumps(result, ensure_ascii=False).encode())
            else:
                self._send(404, b"not found")
        except Exception as e:  # noqa
            self._send(500, json.dumps({"error": str(e)}).encode())


def main():
    # 让中文启动信息在各种控制台编码下都能正确显示
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    port = 8765
    handler = lambda *a, **k: Handler(*a, **k)  # noqa
    # 绑定 0.0.0.0，允许同一局域网内的手机访问
    try:
        httpd = socketserver.ThreadingTCPServer(("0.0.0.0", port), handler)
    except OSError:
        # 端口占用则换一个
        httpd = socketserver.ThreadingTCPServer(("0.0.0.0", 0), handler)
        port = httpd.server_address[1]
    local_url = "http://127.0.0.1:%d/" % port
    lan_ip = get_lan_ip()
    lan_url = "http://%s:%d/" % (lan_ip, port)
    print("=" * 52)
    print("  双色球 / 大乐透 历史数据分析工具 已启动")
    print("  [本机浏览器] %s" % local_url)
    print("  [手机访问]   %s   (手机需与电脑连接同一 WiFi)" % lan_url)
    print("=" * 52)
    print("  提示：若手机无法打开，请放行 Windows 防火墙端口 %d（或允许 Python 联网）。" % port)
    threading.Timer(0.8, lambda: webbrowser.open(local_url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
