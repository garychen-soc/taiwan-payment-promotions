# 台灣支付優惠雷達

[公開網站](https://garychen-soc.github.io/taiwan-payment-promotions/) · [GitHub Repository](https://github.com/garychen-soc/taiwan-payment-promotions)

這個專案定期擷取台灣支付業者的官方活動頁與官方最新消息，將活動生命週期和額度狀態分開判斷，保存可稽核證據，並輸出 Markdown 與 JSON 報表。

## 參考設計與取捨

本專案曾以唯讀方式分析 MIT 授權的 [tw-pay-deals-radar](https://github.com/garychen-soc/tw-pay-deals-radar)。採用的設計方向包含全支付官方 EventId 探索、緊湊的手機優惠卡片、回饋重點提示、深色模式與業者涵蓋檢視；實作則重新整合進本專案既有的官方來源白名單、狀態證據、歷史資料庫、測試及每日發布流程。

以下做法刻意不採用：把 PTT 等非官方內容當成活動證據、以固定 EventId 上限取代可持續前進的探索狀態、吞掉來源錯誤、依不完整日期產生日曆事件，以及把資料硬編碼在靜態 HTML。它們可能造成漏報、錯誤歸屬或無法分辨「查無活動」與「來源故障」。

GitHub Pages 提供手機優先的繁體中文儀表板，可搜尋業者與活動，並依「重點、高回饋、即將開始、即將結束、額滿」篩選。日期完整的活動可開啟預填好的 Google Calendar 全天活動草稿；首頁只顯示簡單更新狀態，來源讀取細節留在結構化資料中，不干擾一般閱讀。

目前來源登錄涵蓋全支付、Pi 拍錢包、PX Pay、My FamiPay、OPEN錢包、悠遊付、台灣 Pay、一卡通 MONEY、icash Pay、全盈+PAY、橘子支付、街口支付、歐付寶、ezPay 與 LINE Pay Money。來源採可擴充登錄制；新增官方業者或活動頁通常只需更新 `config/sources.json`，特殊官方 API 則以具名 adapter 驗證回應結構及品牌歸屬。

Pi 拍錢包透過官方 WordPress API 發現活動與公告，並解碼標題及摘要後再讀取官方詳情；PX Pay 只納入全聯活動卡上明確標為「PX Pay」的項目，另以全聯官方福利點 API 保存各銀行額滿時間或即時剩餘名額。My FamiPay 僅使用全家官網卡片上的日期、標題與摘要，卡片即使連往銀行網站也不會把外部內容當作全家官方證據。OPEN錢包使用 7-ELEVEN 官方支付入口與固定活動頁。這四個來源都保留 `partial` 覆蓋說明，不把公開頁面宣稱成 App 內完整清單。

全支付目前會從官方「百大品牌」與「夏日乾杯飲料季」主題總覽 JSON 發現活動，再沿官方活動詳情的 EventId 連結圖補齊相關活動；此外會以低速、可稽核的官方 API 編號探索找出未連在主題頁上的活動。探索器首次建立基線，之後由 SQLite 分開保存「最高有效 EventId」與「已完整探測前緣」，每日回掃近期區間並讓前緣持續向前；官方 `code=2001` 空號與 DNS／HTTP／JSON 失敗會分開記錄，任何不完整探索都不會推進前緣。已知且未過期的活動仍會每日複查官方詳情、頁面使用的公開額滿狀態 API 與官網最新消息 JSON。

活動編號探索不是官方承諾的完整清單，因此報表仍誠實標為 `partial_public_activity_discovery`，並由每日 AI 官方網域搜尋補查。全支付 App 小鈴鐺的「活動通知」也沒有公開列表，因此只有 App 能查到的額滿狀態會繼續標為 `unknown_app_only`，不會推測仍有名額。報表會分開顯示「官方入口成功 X/Y」與活動詳情、額滿狀態、編號探索等「延伸檢查成功 X/Y」；兩者都不代表 App-only 活動已達絕對完整涵蓋。

## 判斷原則

- `lifecycle`：`upcoming`、`active`、`ended`、`cancelled`。
- `quota_status`：`not_marked_full`、`partial_sold_out`、`sold_out`、`confirmed_available`、`unknown_app_only`。
- 報表排除已過期活動，但 SQLite 仍保留歷史紀錄。
- 單一日期不會被推定為活動結束日；除非頁面明寫「僅限當日」，否則保留為開放結束日並送複核。
- Google Calendar 連結只為開始日、結束日皆明確且順序正確的活動產生；連結會開啟草稿，不會自動寫入使用者行事曆。
- 「限量」、「送完為止」、「若額滿將公告」只是活動規則，不會被判成已額滿。
- 明確的「已額滿／已達上限／已贈完」才是額滿證據。
- 月份或子活動額滿會標為 `partial_sold_out`，不會把整檔活動誤標成額滿。
- 額滿資訊僅在 App 顯示時，標為 `unknown_app_only`，不推論仍有名額。
- 額滿公告關聯優先序：活動 ID／原始 URL，其次才是標題、期別與通路的高信心比對。
- 已有官方證據的 `sold_out`／`partial_sold_out` 會跨輪保留；公告從最新消息列表下架不會造成降級，只有新的明確官方證據才能重新開放或把整檔額滿修正為部分期別額滿。
- 10% 以上百分比回饋標為「高回饋」，固定回饋 100 元以上標為「高額回饋」。消費門檻、個人上限、活動總預算與手續費不會被誤當回饋。
- 未來 14 天開始的活動標為「即將開始」，7 天內截止的活動標為「即將結束」。
- AI 每日從官方資料挑選最多 8 筆重點，摘要高回饋、即將開始、即將結束與已額滿提醒；AI 補充網址仍須通過官方網域白名單。

## 執行

不需安裝第三方套件，使用 Python 3.11 以上即可：

```bash
cd /Users/chenzhiming/Documents/codex/taiwan-payment-promotions-monitor
python3 scripts/run_monitor.py --mode full
python3 scripts/run_monitor.py --mode status
python3 scripts/build_site.py
```

- `full`：掃描活動列表、已知活動頁、官方最新消息／公告並發現新活動。
- `status`：重新檢查資料庫內未過期活動及官方額滿公告來源；資料庫為空時會自動先跑完整掃描。
- 若所有官方來源都無法連線，監測器會將 `transport_status` 標為 `unavailable` 並以 exit code `4` 結束；排程不得用該輪的舊資料繼續建置或發布。

產出位置：

- `data/monitor.sqlite3`：活動歷史、每次執行與來源成功／失敗紀錄。
- `reports/latest.md`：最新繁體中文報表。
- `reports/latest.json`：給 AI 或其他系統使用的結構化結果。
- `reports/YYYYMMDD-HHMMSS-*.{md,json}`：每輪快照。
- `data/ai_supplement.json`：每日 AI 官方來源複核後的首頁重點與補充活動。
- `docs/`：已建置的靜態網站，由 GitHub Pages 直接發布。

本機預覽：

```bash
python3 -m http.server 8765 --directory docs
```

開啟 `http://127.0.0.1:8765/` 即可檢查手機與桌面版畫面。

## 每日更新與發布

Codex 本機排程每天台北時間 08:00 執行一次：

1. 掃描官方活動列表、活動頁與最新公告。
2. 排除已過期活動並更新額滿／部分額滿／App-only 狀態。
3. 由 AI 複核來源缺口，整理高回饋與即將開始、結束的重點。
4. 在本機產生 `docs/`，只將靜態網站與 AI 摘要上傳到公開 Repository。
5. GitHub Actions 將已建置完成的 `docs/` 發布至 GitHub Pages。

OpenAI API 不會在 GitHub Actions 執行，也不需要把任何 API Key 放進 GitHub。排程提示詞位於 `prompts/daily-publish.md`；GitHub Pages 工作流程位於 `.github/workflows/pages.yml`。

## 測試

```bash
cd /Users/chenzhiming/Documents/codex/taiwan-payment-promotions-monitor
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

測試固定台北時間，涵蓋西元／民國日期、跨月簡寫、過期排除、條件語句防誤判、部分額滿、App-only、活動 ID 關聯與不同期別防誤連。

## 維護來源

每個業者必須設定 `official_domains`。擷取器會拒絕跳轉到白名單外的網域，避免把搜尋結果、短網址或第三方內容誤當官方證據。`public_status_coverage` 用來揭露 App-only、動態頁面或合作通路公告等可見性限制。

報表把「網址擷取成功率」與「活動發現完整性」分開呈現。列表沒有發現詳情、發現數正好碰到擷取上限、動態頁無公開 adapter，或業者沒有已驗證的活動列表時，都會列入 `coverage_gaps`；HTTP 100% 成功不會被宣稱成已抓到所有活動。

所有 HTTP 重新導向都在送出下一次請求前逐跳檢查 `official_domains`，不會先連到白名單外網址再拒絕結果。

建議每月至少一次依金管會電子支付機構名單與各支付品牌官方網站檢查來源登錄；行動錢包或銀行自有 Pay 可依實際需求追加。
