<!-- automation-key: tw-payment-promotions-daily-publish-v1 -->
在 `/Users/chenzhiming/Documents/codex/taiwan-payment-promotions-monitor` 執行每日更新：

1. 執行 `python3 scripts/run_monitor.py --mode full --timeout 20`。
2. 讀取 `reports/latest.json`、`reports/latest.md` 與 `config/sources.json`。
3. 針對 `coverage_gaps`、`review_required`、高回饋、即將開始與即將結束活動，用 AI 搜尋官方活動入口、官方最新消息／公告；只採 `official_domains` 內網址，或活動辦法明確指定的主辦銀行／合作通路官網。
4. 更新 `data/ai_supplement.json`：
   - `headline`：一句今日重點。
   - `highlights`：最多 8 筆，包含 `kind`、`provider_id`、`provider_name`、`title`、`summary`、`url`。
   - `supplemental_activities`：只放擷取器漏掉、但已由官方來源確認且尚未過期的活動；包含期間、額滿狀態、條件摘要及證據。
5. 僅在官方文字明確出現已額滿／已達上限／已贈完時標額滿；「限量、送完為止、若額滿」不是額滿證據。App-only 必須標成無法由公開頁面確認。
6. 執行 `python3 scripts/build_site.py`，再確認 `docs/index.html` 與 `docs/data/promotions.json` 存在且 JSON 可解析。
7. 僅上傳本流程產生的 `docs/` 與 `data/ai_supplement.json` 到公開 Repository `garychen-soc/taiwan-payment-promotions` 的 `main`：
   - 優先使用本機 Git；若 `git push` 因本機 GitHub 憑證無效而失敗，不要重複嘗試登入，改用已連結的 GitHub App／connector 建立 blob、tree、commit 並以 non-force update-ref 原子更新 `main`。
   - 提交訊息使用 `Update promotions YYYY-MM-DD`；若遠端內容沒有變更，不建立空提交。
   - 不得改動每日流程範圍外的遠端檔案。
8. 回報當日活動數、高回饋／即將開始／額滿重點、官網讀取失敗、AI 補查缺口，以及 GitHub Pages 網址。

不得提交 SQLite、瀏覽器狀態、權杖、Cookie 或任何憑證。
