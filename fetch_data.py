#!/usr/bin/env python3
"""BTC Daily Watchboard — 每日抓取腳本.

從 bitcoin-data.com 免費 API 抓 5 條序列 (BTC price / realized price / CVDD /
MVRV Z-score / Mayer Multiple)，並自算 balancedPriceCowen = RP − CVDD
（Puell 口徑 Balance Price，與 Cowen/checkonchain ~$38-39k 一致），
輸出 data.json 供靜態頁讀取。

Bitstamp 日 OHLC 另算：200W MA / Pi Cycle / 50W MA（週收 50 期 SMA，含連續根數與
守線門檻）/ lower-low 追蹤（相對 2026-07-01 wick 低點，deadline 2026-12-31）。

⚠ 原生 /v1/balanced-price 端點已棄用（2026-07-12 驗證：其隱含 transferred
price ~$24k 與自家 CVDD ~$13.5k、Puell 定義皆不符，口徑不明）。

免費版限流：每小時 10 次請求。本腳本一次只打 4 個端點 → 每日跑一次完全安全。
失敗時不覆寫舊 data.json（保留上次成功快照）。

Cowen Risk Metric 為 ITC 會員專屬、無公開 API → 由 cowen_risk_manual.json
手動維護（每次跑 Cowen delta 時更新），本腳本不碰它。

Usage: python3 fetch_data.py
"""
import json, sys, time, math, urllib.request, pathlib
from datetime import datetime, timezone, date, timedelta

try:                # 判決樹追蹤（golden cross / 價格帶 / 量能 / 200D 回踩）
    import trackers # 缺檔或語法錯不得拖垮每日核心更新 → decision 區塊自動跳過
except Exception as _e:
    trackers = None
    print(f"[init] trackers 模組載入失敗，decision 區塊將跳過: {_e}", file=sys.stderr)

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "data.json"
DOMF = HERE / "dominance_history.json"
LOGF = HERE / "logs" / "fetch.log"
KEEP_DAYS = 120  # 留 ~4 個月，足夠畫 3-month 走勢

def logmsg(msg):
    """Append a timestamped line to logs/fetch.log (reliable under launchd)."""
    try:
        LOGF.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOGF, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass
    print(msg, file=sys.stderr)

ENDPOINTS = {
    "btcPrice":      ("https://bitcoin-data.com/v1/btc-price",       "btcPrice"),
    "realizedPrice": ("https://bitcoin-data.com/v1/realized-price",  "realizedPrice"),
    "cvdd":          ("https://bitcoin-data.com/v1/cvdd",            "cvdd"),
    "mvrvZscore":    ("https://bitcoin-data.com/v1/mvrv-zscore",     "mvrvZscore"),
    "mayerMultiple": ("https://bitcoin-data.com/v1/mayer-multiple",  "mayerMultiple"),
}

def derive_bp_cowen(series):
    """balancedPriceCowen = realizedPrice − CVDD，按日期 join（Puell 口徑 BP）。"""
    rp = {p["d"]: p["v"] for p in series.get("realizedPrice", [])}
    cv = {p["d"]: p["v"] for p in series.get("cvdd", [])}
    pts = [{"d": d, "v": round(rp[d] - cv[d], 4)}
           for d in sorted(rp.keys() & cv.keys())
           if math.isfinite(rp[d] - cv[d])]
    return pts[-KEEP_DAYS:]

def fetch(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "btc-watchboard/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            print(f"[fetch] {url} attempt {i+1} failed: {e}", file=sys.stderr)
            time.sleep(5)
    return None

def pct(series):
    """回傳 1wk / 1mo / 3mo 變化率 (%)，以最新值對比 N 天前。"""
    if len(series) < 2:
        return {}
    vals = [p["v"] for p in series]
    last = vals[-1]
    out = {}
    for label, n in (("1wk", 7), ("1mo", 30), ("3mo", 90)):
        if len(vals) > n and vals[-1 - n] not in (0, None):
            out[label] = round((last - vals[-1 - n]) / vals[-1 - n] * 100, 2)
    return out

# ---------------------------------------------------------------------------
# 副來源（防禦式）：200W MA / Pi Cycle 需長歷史 → Bitstamp 免費 OHLC 自算；
# BTC Dominance → CoinGecko /global（免費僅當前值，存檔逐日累積）。
# 任一副來源失敗都不可影響核心 4(+Mayer) 條 bitcoin-data 序列與 data.json 寫出。
# ---------------------------------------------------------------------------
def _mean(xs):
    return sum(xs) / len(xs) if xs else None

def _bitstamp_ohlc(params):
    """回傳 [dict(ts,o,h,l,c,v)]；失敗回 []。

    ⚠ 同一 response 就含 OHLCV，全留不多打 API：
      low  → lower-low 追蹤；high → 判決樹的局部高／lower-high 階梯；volume → 量能。
    """
    url = "https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=86400&" + params
    raw = fetch(url)
    rows = []
    if raw and isinstance(raw, dict):
        for row in raw.get("data", {}).get("ohlc", []):
            try:
                ts = int(row["timestamp"])
                o, h = float(row["open"]), float(row["high"])
                lo, c = float(row["low"]), float(row["close"])
                v = float(row["volume"])
            except (KeyError, TypeError, ValueError):
                continue
            if not all(math.isfinite(x) for x in (o, h, lo, c, v)):
                continue
            if c > 0 and lo > 0 and h > 0:
                rows.append({"ts": ts, "o": o, "h": h, "l": lo, "c": c, "v": v})
    return rows

def fetch_bitstamp_daily():
    """兩次分頁取 ~2000 天日 OHLCV；回傳升冪 [dict(d,o,h,l,c,v)] 或 None。"""
    try:
        now = int(time.time())
        recent = _bitstamp_ohlc(f"limit=1000&end={now}")
        if not recent:
            return None
        oldest = min(r["ts"] for r in recent)
        time.sleep(1)
        older = _bitstamp_ohlc(f"limit=1000&start={oldest - 1000*86400}&end={oldest - 86400}")
        merged = {}
        for r in older + recent:
            d = datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%Y-%m-%d")
            merged[d] = {"d": d, "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"]}
        return [merged[d] for d in sorted(merged)]
    except Exception as e:
        logmsg(f"[bitstamp] fail: {e}")
        return None

def to_cl_rows(rich):
    """rich OHLCV → 既有消費者要的 [(date, close, low)] 投影（compute_week_ma50 / lower_low / derived）。"""
    return [(r["d"], r["c"], r["l"]) for r in rich] if rich else None

def fetch_binance_taker(limit=400):
    """Binance 日線 takerBuyBaseVolume → 主動買 vs 主動賣拆分（真正的買賣量能）。

    免 key、與 Bitstamp 獨立；失敗回 None，只影響 decision.volume 的 taker 欄位。
    """
    try:
        raw = fetch(f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit={limit}")
        if not isinstance(raw, list):
            return None
        out = []
        for row in raw:
            try:
                d = datetime.fromtimestamp(row[0] / 1000, timezone.utc).strftime("%Y-%m-%d")
                v, tb = float(row[5]), float(row[9])
            except (IndexError, TypeError, ValueError):
                continue
            if math.isfinite(v) and math.isfinite(tb) and v > 0:
                out.append({"d": d, "v": v, "tb": tb})
        return out or None
    except Exception as e:
        logmsg(f"[binance] fail: {e}")
        return None

# --- 50 週均線（週線口徑）------------------------------------------------
# ⚠ 50W MA 必須用「週收盤的 50 期 SMA」，不可拿 Pi Cycle 現成的 350 日 SMA 代替：
#    兩者目前只差 ~0.3%，但比值貼近 1.000 時足以讓燈號翻面（2026-09-06 比值 1.001）。
WEEK_MA_WINDOW = 50

def _weekly_closes(rows):
    """日 OHLC → [(週收日, 週收, 是否已收完)]；ISO 週一~週日，週收 = 該週週日日收。"""
    buckets = {}
    for d, c, _lo in rows:
        y, w, wd = date.fromisoformat(d).isocalendar()
        buckets.setdefault((y, w), {})[wd] = (d, c)
    out = []
    for key in sorted(buckets):
        days = buckets[key]
        d, c = days[max(days)]
        out.append((d, c, 7 in days))      # 有週日才算收完
    return out

def compute_week_ma50(rows):
    """50 週均線 + 連續週收根數 + 未來三週守線門檻 + 進行中週的即時對照。

    ⚠ 今日（UTC）的日蠟燭仍在形成，Bitstamp 會回一根「當前價」當 close。
    週收訊號一律只採【已收完】的週：先剔除 today(UTC)，該週要有週日才算收完。
    """
    W = WEEK_MA_WINDOW
    today = datetime.now(timezone.utc).date().isoformat()
    final = [r for r in rows if r[0] < today]        # 排除進行中的當日蠟燭
    if not final:
        return None
    wk = _weekly_closes(final)
    if len(wk) < W:
        return None
    ma = [None] * len(wk)
    for i in range(W - 1, len(wk)):
        ma[i] = _mean([wk[j][1] for j in range(i - W + 1, i + 1)])

    closed = [i for i in range(len(wk)) if wk[i][2] and ma[i] is not None]
    if not closed:
        return None
    li = closed[-1]
    ld, lc = wk[li][0], wk[li][1]
    lma = ma[li]

    # 連續同向根數（只認已收完的週）
    streak, sign = 0, None
    for i in reversed(closed):
        s = wk[i][1] > ma[i]
        if sign is None:
            sign = s
        if s != sign:
            break
        streak += 1

    # 守線門檻：k 週後若價格維持 p，MA=(sum(win[k:])+k*p)/W → 需 p > sum(win[k:])/(W-k)
    win = [wk[j][1] for j in range(li - W + 1, li + 1)]
    breakeven = [{"k": k, "p": round(sum(win[k:]) / (W - k), 2)} for k in (1, 2, 3)]

    # 日 forward-fill 序列（供 Tab1 走勢圖）：每日套用「當日或之前最近一根已收週」的 MA
    ma_by_week = [(wk[i][0], ma[i]) for i in closed]
    dates = [r[0] for r in rows]
    ser, p = [], 0
    for d in dates[-KEEP_DAYS:]:
        while p + 1 < len(ma_by_week) and ma_by_week[p + 1][0] <= d:
            p += 1
        if ma_by_week[p][0] <= d:
            ser.append({"d": d, "v": round(ma_by_week[p][1], 2)})

    # 進行中的這一週：若「現在」就收盤會落在 MA 的哪一邊（不計入 streak）
    live = None
    last_d, last_p = rows[-1][0], rows[-1][1]
    if last_d > ld:
        wd = date.fromisoformat(last_d).isocalendar()[2]
        closes_on = (date.fromisoformat(last_d) + timedelta(days=7 - wd)).isoformat()
        be1 = breakeven[0]["p"]
        live = {
            "d": last_d,
            "closesOn": closes_on,
            "price": round(last_p, 2),
            "maIfClosedNow": round((sum(win[1:]) + last_p) / W, 2),
            "breakeven": be1,
            "wouldBeAbove": bool(last_p > be1),
        }

    return {
        "current": round(lma, 2),
        "series": ser,
        "live": live,
        "week": {
            "d": ld,
            "close": round(lc, 2),
            "ma": round(lma, 2),
            "ratio": round(lc / lma, 4),
            "above": bool(lc > lma),
            "streak": streak,
            "streakDir": "above" if sign else "below",
            "target": 3,                      # 3 根連續週收 = 轉勢確認
            "breakeven": breakeven,
        },
    }

# --- 年底前有無 lower low -------------------------------------------------
# 地板 = 2026-07-01 的 wick 低點（實測 $57,735；使用者口徑「7/1 低點 57,000」為整數線）。
# 每次執行都從 Bitstamp 完整歷史重算，不存狀態檔 → 某日抓取失敗隔天自動修復。
LL_ANCHOR   = "2026-07-01"
LL_FLOOR_RD = 57000.0        # 使用者指定整數地板（較實測 wick 低 1.3%，留跨交易所噪音緩衝）
LL_DEADLINE = "2026-12-31"

def compute_lower_low(rows):
    """lower-low 追蹤：wick 破 = tested(🟡)、日收破整數地板 = broken(🔴)。"""
    by_date = {d: (c, lo) for d, c, lo in rows}
    if LL_ANCHOR not in by_date:
        return None
    floor_wick = by_date[LL_ANCHOR][1]
    after = [(d, c, lo) for d, c, lo in rows if d > LL_ANCHOR]
    if not after:
        return None

    min_low, min_low_d = min((lo, d) for d, _c, lo in after)
    min_close, min_close_d = min((c, d) for d, c, _lo in after)
    wick_breach = next((d for d, _c, lo in after if lo < floor_wick), None)
    close_breach = next((d for d, c, _lo in after if c < LL_FLOOR_RD), None)
    state = "broken" if close_breach else ("tested" if wick_breach else "clear")

    last_d, last_c = rows[-1][0], rows[-1][1]
    a  = date.fromisoformat(LL_ANCHOR)
    dl = date.fromisoformat(LL_DEADLINE)
    ld = date.fromisoformat(last_d)
    days_total = (dl - a).days
    days_held  = max((min(ld, dl) - a).days, 0)
    days_left  = max((dl - ld).days, 0)

    return {
        "anchorDate": LL_ANCHOR,
        "floorWick": round(floor_wick, 2),
        "floorRound": LL_FLOOR_RD,
        "deadline": LL_DEADLINE,
        "state": state,
        "wickBreachDate": wick_breach,
        "closeBreachDate": close_breach,
        "minLow": round(min_low, 2), "minLowDate": min_low_d,
        "minClose": round(min_close, 2), "minCloseDate": min_close_d,
        "lastDate": last_d, "lastClose": round(last_c, 2),
        "daysHeld": days_held, "daysTotal": days_total, "daysLeft": days_left,
        "progress": round(days_held / days_total, 4) if days_total else None,
        "ddToWick": round(floor_wick / last_c - 1, 4),
        "ddToRound": round(LL_FLOOR_RD / last_c - 1, 4),
        "expired": ld >= dl,
    }

def compute_derived(rows):
    """從日 OHLC 算 200W MA / Pi Cycle / 50W MA(週線) / lower-low（current + 近 120 天 series）。"""
    if not rows or len(rows) < 350:
        return None
    dates = [r[0] for r in rows]
    vals  = [r[1] for r in rows]
    n = len(vals)

    def ma(window, end_idx):
        start = end_idx - window + 1
        if start < 0:
            return None
        return _mean(vals[start:end_idx + 1])

    ma200w_cur  = ma(1400, n - 1)            # <1400 天回 None
    ma111_cur   = ma(111, n - 1)
    ma350_cur   = ma(350, n - 1)
    ma350x2_cur = ma350_cur * 2 if ma350_cur else None
    ratio_cur   = (ma111_cur / ma350x2_cur) if (ma111_cur and ma350x2_cur) else None

    span = min(120, n)
    ser200, serRatio = [], []
    for j in range(n - span, n):
        m2 = ma(1400, j)
        if m2 is not None:
            ser200.append({"d": dates[j], "v": round(m2, 2)})
        m111 = ma(111, j); m350 = ma(350, j)
        if m111 and m350:
            serRatio.append({"d": dates[j], "v": round(m111 / (2 * m350), 4)})

    out = {
        "ma200w": {"current": round(ma200w_cur, 2) if ma200w_cur else None, "series": ser200},
        "piCycle": {
            "ma111": round(ma111_cur, 2) if ma111_cur else None,
            "ma350x2": round(ma350x2_cur, 2) if ma350x2_cur else None,
            "ratio": round(ratio_cur, 4) if ratio_cur else None,
            "triggered": bool(ma111_cur and ma350x2_cur and ma111_cur >= ma350x2_cur),
            "series_ratio": serRatio,
        },
        "bitstamp_through": dates[-1],
        "history_days": n,
    }
    # 兩張 hero 卡：50W MA 週收確認 / 年底前無 lower low（任一失敗不影響其餘 derived）
    for key, fn in (("ma50w", compute_week_ma50), ("lowerLow", compute_lower_low)):
        try:
            v = fn(rows)
            if v is not None:
                out[key] = v
            else:
                logmsg(f"[derived] {key} 跳過（資料不足）")
        except Exception as e:
            logmsg(f"[derived] {key} 例外: {e}")
    return out

def fetch_dominance():
    raw = fetch("https://api.coingecko.com/api/v3/global")
    try:
        v = float(raw["data"]["market_cap_percentage"]["btc"])
        return round(v, 2) if math.isfinite(v) else None
    except (TypeError, KeyError, ValueError):
        return None

def update_dominance_file(dom):
    """按日 idempotent 累積 BTC.D；回傳最新值或 None。"""
    if dom is None:
        return None
    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    try:
        hist = json.loads(DOMF.read_text()) if DOMF.exists() else {"points": []}
    except Exception:
        hist = {"points": []}
    pts = hist.get("points", [])
    if not any(p.get("d") == today for p in pts):
        pts.append({"d": today, "v": dom})
    pts.sort(key=lambda x: x["d"])
    pts = pts[-KEEP_DAYS:]
    hist["points"] = pts
    hist["note"] = "BTC Dominance (%) — CoinGecko /global；歷史自部署日起逐日累積（免費版無歷史）"
    DOMF.write_text(json.dumps(hist, ensure_ascii=False, indent=2, allow_nan=False))
    return pts[-1]["v"] if pts else None

def main():
    series, current, changes = {}, {}, {}
    ok = True
    for key, (url, field) in ENDPOINTS.items():
        raw = fetch(url)
        if not raw or not isinstance(raw, list):
            print(f"[main] {key} 抓取失敗，保留舊快照", file=sys.stderr)
            ok = False
            break
        pts = []
        for row in raw:
            d = row.get("d")
            v = row.get(field)
            if d is None or v in (None, ""):
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(fv):   # 跳過 NaN / Inf（API 偶發壞點，否則寫出非法 JSON）
                continue
            pts.append({"d": d, "v": round(fv, 4)})
        pts.sort(key=lambda x: x["d"])
        pts = pts[-KEEP_DAYS:]
        series[key] = pts
        if pts:
            current[key] = pts[-1]["v"]
            changes[key] = pct(pts)
        time.sleep(1)

    if not ok:
        logmsg("[main] FAIL — 有端點失敗（多半是 429 限流），保留舊快照不覆寫")
        sys.exit(1)

    # --- 衍生序列：Puell 口徑 Balance Price = RP − CVDD ---
    bp_pts = derive_bp_cowen(series)
    if bp_pts:
        series["balancedPriceCowen"] = bp_pts
        current["balancedPriceCowen"] = bp_pts[-1]["v"]
        changes["balancedPriceCowen"] = pct(bp_pts)
    else:
        logmsg("[main] balancedPriceCowen 跳過（RP/CVDD 日期無交集）")

    # --- 副來源（各自 try/except；失敗不阻擋核心 data.json 寫出）---
    derived = None
    try:
        rich = fetch_bitstamp_daily()
        derived = compute_derived(to_cl_rows(rich))
        if derived is None:
            logmsg("[main] derived 跳過（Bitstamp 無資料或歷史不足）")
        elif rich and trackers:
            # 判決樹追蹤（golden cross / 價格帶 / 量能 / 200D 回踩）；失敗不影響其餘 derived
            try:
                dec = trackers.build(rich, fetch_binance_taker())
                if dec:
                    derived["decision"] = dec
            except Exception as e:
                logmsg(f"[main] decision 例外: {e}")
    except Exception as e:
        logmsg(f"[main] derived 例外: {e}")
    if derived is None and OUT.exists():     # carry-forward 前一份
        try:
            prev = json.loads(OUT.read_text())
            if isinstance(prev.get("derived"), dict):
                derived = {**prev["derived"], "stale": True}
                logmsg("[main] derived 沿用前一份（標記 stale）")
        except Exception:
            pass

    try:
        dom = update_dominance_file(fetch_dominance())
        if dom is not None:
            current["btcDominance"] = dom
        else:
            logmsg("[main] dominance 跳過")
    except Exception as e:
        logmsg(f"[main] dominance 例外: {e}")

    data_through = max((s[-1]["d"] for s in series.values() if s), default=None)

    # freshness：on-chain 正常落後約 2 天；> STALE_LAG_DAYS 視為過期（供前端 banner + daily.yml gate）
    STALE_LAG_DAYS = 3
    lag_days = None
    if data_through:
        try:
            dt = datetime.strptime(data_through, "%Y-%m-%d").date()
            lag_days = (datetime.now(timezone.utc).date() - dt).days
        except Exception:
            lag_days = None
    stale = (lag_days is not None and lag_days > STALE_LAG_DAYS)

    out = {
        "data_through": data_through,
        "source": "bitcoin-data.com (free) + Bitstamp(200W/Pi/50W/lower-low) + CoinGecko(BTC.D)",
        "freshness": {"lag_days": lag_days, "stale": stale, "threshold": STALE_LAG_DAYS},
        "series": series,
        "current": current,
        "changes": changes,
    }
    if derived is not None:
        out["derived"] = derived
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False))
    if stale:
        logmsg(f"::warning::資料已 {lag_days} 天未更新（>{STALE_LAG_DAYS}），上游可能限流/中斷")
    logmsg(f"[main] OK — data_through={data_through} (lag={lag_days}d), {len(series)} series x {KEEP_DAYS}d"
           f", derived={'y' if derived else 'n'}, btcDom={current.get('btcDominance')}")

if __name__ == "__main__":
    main()
