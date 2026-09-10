"""判決樹追蹤器 — 把 Cowen 9/07+9/08 的檢查點與價格帶研判做成每日可機械結算的狀態。

設計原則（沿用本專案既有教訓）：
- **進行中的蠟燭要標記**：今日(UTC) 日 K 仍在形成，任何「已確認」判定只採已收盤日；
  進行中的讀數另放 live 區塊（同 compute_week_ma50 的防呆）。
- **不存狀態檔**：每次從完整歷史重算，抓失敗隔天自動修復。
- **口徑分離**：Cowen 口播的歷史判例值標為 stated（未獨立驗證）；本檔只機械計算「當前這一輪」。

輸入 rows 為升冪 [{d, o, h, l, c, v}]（Bitstamp 日 OHLCV），
taker 為 Binance 日線的買賣拆分 [{d, v, tb}]（可為 None，僅影響 volume 區塊）。
"""

import math
from datetime import date, timedelta

# --- 常數：本輪判決樹的錨點 -------------------------------------------------
# 參考高 = golden cross 之前的局部高；Cowen 9/08 口播 82,292（4H），Binance 日線 82,300 @ 2026-09-03。
REF_HIGH_SEARCH_FROM = "2026-08-15"   # 找局部高的起點（夏季低反彈段之後）
MAY_HIGH = {"d": "2026-05-06", "v": 82850.0, "note": "2026-05 高；lower-high 階梯的上緣"}

# Cowen 9/08 口播的四個歷史判例（stated＝他的口徑，本檔未獨立驗證）
GC_PRECEDENTS = [
    {"year": 2014, "pct": (-8.0, -9.0),   "outcome": "lower_high",  "note": "期中年；交叉後 lower high → 10 月低"},
    {"year": 2015, "pct": (-13.0, -13.0), "outcome": "lower_high",  "note": "前減半年；反彈到 lower high 後續跌"},
    {"year": 2019, "pct": (-14.0, -15.0), "outcome": "higher_high", "note": "前減半年；反彈創 higher high"},
    {"year": 2023, "pct": (-12.0, -12.0), "outcome": "higher_high", "note": "前減半年；先回踩 200D 後轉多"},
]

# 裁決期限：Cowen 9/08「that week or two」自交叉日起算
RALLY_TEST_DEADLINE_DAYS = 14

# 其他 KOL 的線（外部輸入值，非本檔計算）
KOL_LINES = {
    "fage_weighted_cost": {"v": 79600.0, "label": "發哥 市場資本加權平均成本", "source": "使用者轉述，未獨立驗證"},
}

# 使用者關注的兩個價格帶
BANDS = [
    {"key": "A", "lo": 72000.0, "hi": 75000.0, "label": "72–75K"},
    {"key": "B", "lo": 70000.0, "hi": 72000.0, "label": "70–72K（判例尾端＋200D 區）"},
    {"key": "C", "lo": 0.0,     "hi": 70000.0, "label": "<70K"},
]


def _f(x, n=2):
    return round(x, n) if (x is not None and math.isfinite(x)) else None


def _closed_rows(rows, today_utc):
    """剔除今日(UTC)這根仍在形成的蠟燭。"""
    return [r for r in rows if r["d"] < today_utc]


def _sma(vals, i, w):
    if i - w + 1 < 0:
        return None
    return sum(vals[i - w + 1:i + 1]) / w


# --- 1) 黃金交叉偵測 --------------------------------------------------------
def find_golden_cross(rows, lookback=400):
    """最近一次 50D SMA 上穿 200D SMA 的日期（只採已收盤日）。"""
    closes = [r["c"] for r in rows]
    n = len(closes)
    if n < 210:
        return None
    last = None
    start = max(200, n - lookback)
    for i in range(start, n):
        m50, m200 = _sma(closes, i, 50), _sma(closes, i, 200)
        p50, p200 = _sma(closes, i - 1, 50), _sma(closes, i - 1, 200)
        if None in (m50, m200, p50, p200):
            continue
        if p50 <= p200 and m50 > m200:
            last = {"date": rows[i]["d"], "ma50": _f(m50), "ma200": _f(m200)}
    return last


# --- 2) 判決樹狀態機 --------------------------------------------------------
def compute_decision_tree(rows, today_utc=None):
    """golden cross → dump → post-dump rally → higher/lower high 的機械結算。

    狀態：
      pre_cross  — 尚未偵測到交叉
      dumping    — 自參考高回落中，尚未出現足以認定的反彈起點
      rallying   — 已自 dump low 反彈，等待 resolve
      higher_high— 反彈高 > 參考高（Cowen：bear case substantially weaker）
      lower_high — 反彈為 lower high 且已跌破 dump low（Cowen：drop into Q4 的論據）
    """
    today_utc = today_utc or date.today().isoformat()
    closed = _closed_rows(rows, today_utc)
    if len(closed) < 210:
        return None

    gc = find_golden_cross(closed)
    if not gc:
        return {"state": "pre_cross"}

    # 參考高 = 交叉前的局部高（自 REF_HIGH_SEARCH_FROM 起、至交叉日）
    seg = [r for r in closed if REF_HIGH_SEARCH_FROM <= r["d"] <= gc["date"]]
    if not seg:
        return {"state": "pre_cross"}
    ref = max(seg, key=lambda r: r["h"])
    ref_high = {"d": ref["d"], "v": _f(ref["h"])}

    # 自參考高之後的走勢
    after = [r for r in closed if r["d"] > ref["d"]]
    if not after:
        return {"state": "dumping", "goldenCross": gc, "refHigh": ref_high}

    dump_row = min(after, key=lambda r: r["l"])
    dump_low = {"d": dump_row["d"], "v": _f(dump_row["l"])}
    dump_pct = (dump_row["l"] / ref["h"] - 1) * 100

    # dump low 之後的反彈高
    post = [r for r in after if r["d"] > dump_row["d"]]
    rally_high = None
    if post:
        rh = max(post, key=lambda r: r["h"])
        rally_high = {"d": rh["d"], "v": _f(rh["h"]),
                      "pctOffDumpLow": _f((rh["h"] / dump_row["l"] - 1) * 100)}

    # 反彈後是否又跌破 dump low（lower high 確認的第二隻腳）
    broke_dump_low = False
    if rally_high:
        for r in post:
            if r["d"] > rally_high["d"] and r["l"] < dump_row["l"]:
                broke_dump_low = True
                break

    if rally_high and rally_high["v"] > ref["h"]:
        state, verdict = "higher_high", "熊市論點顯著轉弱（Cowen 9/08 多方分支）"
    elif rally_high and broke_dump_low:
        state, verdict = "lower_high", "lower high 成立 → Q4 下殺論據（Cowen 9/08 空方分支）"
    elif rally_high and rally_high["pctOffDumpLow"] >= 3.0:
        state, verdict = "rallying", "反彈進行中，尚未 resolve"
    else:
        state, verdict = "dumping", "下殺進行中，尚未出現反彈起點"

    deadline = (date.fromisoformat(gc["date"]) + timedelta(days=RALLY_TEST_DEADLINE_DAYS)).isoformat()
    days_left = (date.fromisoformat(deadline) - date.fromisoformat(today_utc)).days

    # 判例門檻換算（自參考高）
    prec = []
    for p in GC_PRECEDENTS:
        lo_pct, hi_pct = p["pct"]
        p_lo = ref["h"] * (1 + lo_pct / 100)
        p_hi = ref["h"] * (1 + hi_pct / 100)
        deepest = min(p_lo, p_hi)
        prec.append({
            "year": p["year"],
            "pct": list(p["pct"]),
            "priceHi": _f(max(p_lo, p_hi)),
            "priceLo": _f(deepest),
            "reached": bool(dump_row["l"] <= max(p_lo, p_hi)),
            "outcome": p["outcome"],
            "note": p["note"],
            "stated": True,
        })
    reached_n = sum(1 for p in prec if p["reached"])

    return {
        "state": state,
        "verdict": verdict,
        "goldenCross": gc,
        "refHigh": ref_high,
        "mayHigh": MAY_HIGH,
        "lowerHighGapPct": _f((ref["h"] / MAY_HIGH["v"] - 1) * 100),
        "dumpLow": dump_low,
        "dumpPctFromHigh": _f(dump_pct),
        "rallyHigh": rally_high,
        "brokeDumpLow": broke_dump_low,
        "deadline": deadline,
        "daysLeft": days_left,
        "precedents": prec,
        "precedentsReached": f"{reached_n}/{len(prec)}",
    }


# --- 3) 價格帶研判 ----------------------------------------------------------
def compute_bands(rows, decision, today_utc=None):
    """兩個關注帶（72–75K / <70K）+ 中間縫（70–72K）的距離、是否觸及、判例含意。"""
    today_utc = today_utc or date.today().isoformat()
    closed = _closed_rows(rows, today_utc)
    if not closed or not decision or "refHigh" not in decision:
        return None
    last = closed[-1]
    ref_h = decision["refHigh"]["v"]

    # 自參考高之後的最低 wick（含今日進行中，另標）
    after_closed = [r for r in closed if r["d"] > decision["refHigh"]["d"]]
    min_closed = min((r["l"] for r in after_closed), default=None)
    live_row = next((r for r in rows if r["d"] == today_utc), None)
    min_live = min([x for x in [min_closed, live_row["l"] if live_row else None] if x is not None], default=None)

    out = []
    for b in BANDS:
        touched = bool(min_live is not None and min_live <= b["hi"])
        # 到達該帶上緣所需跌幅（自最新收盤）
        need = (b["hi"] / last["c"] - 1) * 100
        # 該帶對應的「自參考高跌幅」
        from_high_hi = (b["hi"] / ref_h - 1) * 100
        from_high_lo = (b["lo"] / ref_h - 1) * 100 if b["lo"] > 0 else None
        # 有幾個判例的下殺深度會落進/穿過這個帶
        n_prec = sum(1 for p in decision["precedents"] if p["priceLo"] <= b["hi"])
        out.append({
            "key": b["key"], "label": b["label"], "lo": b["lo"], "hi": b["hi"],
            "touched": touched,
            "needPctFromLast": _f(need),
            "fromRefHighPct": [_f(from_high_lo), _f(from_high_hi)],
            "precedentsInOrBelow": n_prec,
            "precedentTotal": len(decision["precedents"]),
        })
    return {
        "asOfClose": {"d": last["d"], "c": _f(last["c"])},
        "minLowSinceRefHigh": _f(min_closed),
        "minLowIncludingLive": _f(min_live),
        "bands": out,
    }


# --- 4) 量能（含買賣拆分）---------------------------------------------------
def compute_volume(rows, taker=None, today_utc=None, win=30):
    """成交量 z-score、漲跌日量能比、taker 買方占比（真正的買賣量能）。

    Cowen 框架：底部收尾需要 volume spike（低量磨底 → 爆量投降）。
    這裡只給可量測讀數與門檻，不做方向判斷。
    """
    today_utc = today_utc or date.today().isoformat()
    closed = _closed_rows(rows, today_utc)
    if len(closed) < win + 5:
        return None
    vols = [r["v"] for r in closed]
    recent = vols[-win:]
    mean = sum(recent) / len(recent)
    var = sum((x - mean) ** 2 for x in recent) / len(recent)
    sd = math.sqrt(var)
    last = closed[-1]
    z = (last["v"] - mean) / sd if sd > 0 else None

    # 近 10 日：上漲日 vs 下跌日成交量
    seg = closed[-10:]
    up_v = sum(r["v"] for r in seg if r["c"] >= r["o"])
    dn_v = sum(r["v"] for r in seg if r["c"] < r["o"])
    ratio = (up_v / dn_v) if dn_v > 0 else None

    out = {
        "asOfClose": last["d"],
        "lastVolume": _f(last["v"], 1),
        "mean30": _f(mean, 1),
        "z": _f(z, 2) if z is not None else None,
        "spike": bool(z is not None and z >= 2.0),
        "spikeThreshold": 2.0,
        "up10dVolume": _f(up_v, 1),
        "down10dVolume": _f(dn_v, 1),
        "upDownVolRatio": _f(ratio, 2) if ratio else None,
        "source": "Bitstamp BTC/USD 日線",
    }

    if taker:
        tk = [t for t in taker if t["d"] < today_utc and t["v"] > 0]
        if tk:
            lastt = tk[-1]
            out["takerBuyPct"] = _f(100 * lastt["tb"] / lastt["v"], 1)
            seg5 = tk[-5:]
            sv = sum(t["v"] for t in seg5)
            out["takerBuyPct5d"] = _f(100 * sum(t["tb"] for t in seg5) / sv, 1) if sv > 0 else None
            seg20 = tk[-20:]
            sv20 = sum(t["v"] for t in seg20)
            out["takerBuyPct20d"] = _f(100 * sum(t["tb"] for t in seg20) / sv20, 1) if sv20 > 0 else None
            out["takerSource"] = "Binance BTCUSDT 日線 takerBuyBaseVolume（主動買 vs 主動賣）"
            out["takerAsOf"] = lastt["d"]
    live = next((r for r in rows if r["d"] == today_utc), None)
    if live:
        out["live"] = {"d": live["d"], "volumeSoFar": _f(live["v"], 1),
                       "note": "今日(UTC)未收盤，量能仍在累積，不計入 z-score"}
    return out


# --- 5) 200D MA 回踩兩週測試（8/21 被架空的檢查點，若回踩即復活）------------
def compute_200d_retrace(rows, today_utc=None):
    today_utc = today_utc or date.today().isoformat()
    closed = _closed_rows(rows, today_utc)
    if len(closed) < 210:
        return None
    closes = [r["c"] for r in closed]
    n = len(closes)
    ma200 = _sma(closes, n - 1, 200)
    last = closed[-1]
    # 自 200D 上方以來，連續收在其上的天數
    streak = 0
    for i in range(n - 1, 199, -1):
        m = _sma(closes, i, 200)
        if m and closes[i] >= m:
            streak += 1
        else:
            break
    return {
        "ma200d": _f(ma200),
        "lastClose": _f(last["c"]),
        "aboveBy": _f((last["c"] / ma200 - 1) * 100) if ma200 else None,
        "closesAboveStreak": streak,
        "testWindowDays": 14,
        "note": "Cowen 8/21 檢查點：第一次回檔能否守住 200D 兩週（2019 守不住→創新低；2023 守住→轉多）",
    }


def build(rows, taker=None, today_utc=None):
    """組裝整個 decision 區塊；任一子項失敗不影響其餘。"""
    today_utc = today_utc or date.today().isoformat()
    out = {"asOf": today_utc, "kolLines": KOL_LINES}
    dec = None
    for key, fn in (("tree", lambda: compute_decision_tree(rows, today_utc)),
                    ("volume", lambda: compute_volume(rows, taker, today_utc)),
                    ("ma200d", lambda: compute_200d_retrace(rows, today_utc))):
        try:
            v = fn()
            if v is not None:
                out[key] = v
                if key == "tree":
                    dec = v
        except Exception as e:  # noqa: BLE001
            out.setdefault("errors", []).append(f"{key}: {e}")
    if dec and "refHigh" in dec:
        try:
            b = compute_bands(rows, dec, today_utc)
            if b:
                out["bands"] = b
        except Exception as e:  # noqa: BLE001
            out.setdefault("errors", []).append(f"bands: {e}")
    return out
