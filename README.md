# BTC Daily Watchboard

每日 BTC 看板。雙標籤：

- **Tab 1 — 核心指標日板**：Realized Price / Balance Price / MVRV Z-Score / Cowen Risk Metric，含 1wk·1mo·3mo 走勢與公式解讀。
- **Tab 2 — Cowen 見底訊號狀態板**：12 個 Cowen 判讀指標，每卡含資料型態、白話、見底訊號燈。

**Live**: https://medfoxtaiwan.github.io/btc-daily-watchboard/

## 資料更新
- `data.json`（Realized/Balance Price、MVRV-Z、BTC 價）由 GitHub Actions 每日 00:00 UTC（08:00 台灣）跑 `fetch_data.py` 自雲端更新（來源 bitcoin-data.com 免費 API）。
- `tab2_signals.json` 與 `cowen_risk_manual.json` 為手動維護（Cowen 影片口徑），每週更新。

數據僅供研究參考，非投資建議。
