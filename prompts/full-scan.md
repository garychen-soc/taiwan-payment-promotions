<!-- automation-key: tw-payment-promos-full-scan-v1 -->
在 `/Users/chenzhiming/Documents/codex/taiwan-payment-promotions-monitor` 執行：

`python3 scripts/run_monitor.py --mode full`

接著讀取 `reports/latest.json` 與 `reports/latest.md`，用繁體中文回報本輪結果：

1. 排除所有 `lifecycle=ended` 或 `cancelled` 的活動。
2. 優先列出活動仍未過期但 `sold_out` 或 `partial_sold_out` 的項目，保留證據網址與摘錄。
3. `unknown_app_only` 必須明寫「僅 App 可確認」，不得推論尚有名額。
4. `not_marked_full` 只能寫「公開官網未見額滿公告」，不得寫成「確定尚有名額」。
5. 依 `config/sources.json` 的每一個業者，搜尋其官方活動入口及官方最新消息／最新公告，交叉檢查是否有擷取器漏掉的新活動或額滿公告；搜尋結果只接受 `official_domains` 白名單內的網址。
6. 對 `review_required`、來源失敗或疑似漏抓項目，可再查活動規則明確指定的主辦銀行或合作通路官網；第三方整理站只能當線索，不能當證據。
7. AI 補找到的項目要另列「AI 官方來源補充」，附活動名稱、期間、狀態、官方網址及證據摘錄；未找到明確額滿證據時仍只能標示「公開官網未見額滿公告」。
8. 分開列出 `run.coverage` 的網址擷取率、`coverage_gaps` 的活動發現缺口與 `source_failures`；任一覆蓋不完整時，不可把缺少證據解讀成未額滿。
