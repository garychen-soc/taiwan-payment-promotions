<!-- automation-key: tw-payment-promos-quota-check-v1 -->
在 `/Users/chenzhiming/Documents/codex/taiwan-payment-promotions-monitor` 執行：

`python3 scripts/run_monitor.py --mode status`

接著讀取 `reports/latest.json`。只在下列任一情況發出精簡的繁體中文更新：

- 本輪找到 `sold_out` 或 `partial_sold_out`，並附官方證據網址與摘錄。
- 額滿狀態相較資料庫既有紀錄有變化。
- 來源失敗導致本輪覆蓋不完整。
- `coverage_gaps` 有新增或仍未排除的活動發現缺口。
- 有 `review_required`，且能用業者官網、官網最新消息／公告，或活動規則指定的主辦銀行／合作通路官網取得明確結論。

不要列已過期活動；`unknown_app_only` 必須明寫僅 App 可確認；「限量、送完為止、若額滿」不是已額滿證據。
