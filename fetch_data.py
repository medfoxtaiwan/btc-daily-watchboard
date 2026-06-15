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

    data_through = max((s[-1]["d"] for s in series.values() if s), default=None)
    out = {
        "data_through": data_through,
        "source": "bitcoin-data.com (free)",
        "series": series,
        "current": current,
        "changes": changes,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False))
    logmsg(f"[main] OK — data_through={data_through}, {len(series)} series x {KEEP_DAYS}d")

if __name__ == "__main__":
    main()
