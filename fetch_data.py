#!/usr/bin/env python3
"""BTC Daily Watchboard — 每日抓取腳本.

從 bitcoin-data.com 免費 API 抓 4 條序列 (realized price / balanced price /
MVRV Z-score / BTC price)，輸出 data.json 供靜態頁讀取。

免費版限流：每小時 10 次請求。本腳本一次只打 4 個端點 → 每日跑一次完全安全。
失敗時不覆寫舊 data.json（保留上次成功快照）。

Cowen Risk Metric 為 ITC 會員專屬、無公開 API → 由 cowen_risk_manual.json
手動維護（每次跑 Cowen delta 時更新），本腳本不碰它。

Usage: python3 fetch_data.py
"""
import json, sys, time, math, urllib.request, pathlib
from datetime import datetime, timezone

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
    "balancedPrice": ("https://bitcoin-data.com/v1/balanced-price",  "balancedPrice"),
    "mvrvZscore":    ("https://bitcoin-data.com/v1/mvrv-zscore",     "mvrvZscore"),
    "mayerMultiple": ("https://bitcoin-data.com/v1/mayer-multiple",  "mayerMultiple"),
}

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
    """回傳 [(ts, close)]；失敗回 []。"""
    url = "https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=86400&" + params
    raw = fetch(url)
    rows = []
    if raw and isinstance(raw, dict):
        for row in raw.get("data", {}).get("ohlc", []):
            try:
                ts = int(row["timestamp"]); c = float(row["close"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(c) and c > 0:
                rows.append((ts, c))
    return rows

def fetch_bitstamp_daily_closes():
    """兩次分頁取 ~2000 天日收盤；回傳升冪 [(date, close)] 或 None。"""
    try:
        now = int(time.time())
        recent = _bitstamp_ohlc(f"limit=1000&end={now}")
        if not recent:
            return None
        oldest = min(ts for ts, _ in recent)
        time.sleep(1)
        older = _bitstamp_ohlc(f"limit=1000&start={oldest - 1000*86400}&end={oldest - 86400}")
        merged = {}
        for ts, c in older + recent:
            d = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
            merged[d] = c
        return sorted(merged.items())
    except Exception as e:
        logmsg(f"[bitstamp] fail: {e}")
        return None

def compute_derived(closes):
    """從日收盤算 200W MA + Pi Cycle ratio（current + 近 120 天 series）。"""
    if not closes or len(closes) < 350:
        return None
    dates = [d for d, _ in closes]
    vals  = [v for _, v in closes]
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

    return {
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

    # --- 副來源（各自 try/except；失敗不阻擋核心 data.json 寫出）---
    derived = None
    try:
        derived = compute_derived(fetch_bitstamp_daily_closes())
        if derived is None:
            logmsg("[main] derived 跳過（Bitstamp 無資料或歷史不足）")
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
    out = {
        "data_through": data_through,
        "source": "bitcoin-data.com (free) + Bitstamp(200W/Pi) + CoinGecko(BTC.D)",
        "series": series,
        "current": current,
        "changes": changes,
    }
    if derived is not None:
        out["derived"] = derived
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False))
    logmsg(f"[main] OK — data_through={data_through}, {len(series)} series x {KEEP_DAYS}d"
           f", derived={'y' if derived else 'n'}, btcDom={current.get('btcDominance')}")

if __name__ == "__main__":
    main()
