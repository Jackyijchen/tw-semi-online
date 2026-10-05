# 台股半導體通路量化儀表板 v3.0 — GitHub 線上版

不需要電腦或手機一直開著。GitHub 會在每個交易日台北時間 19:30（收盤後 6 小時）自動抓取並交叉驗證資料，更新網頁。網址固定為：

```
https://<你的帳號>.github.io/tw-semi-online/
```

用手機或電腦瀏覽器打開即可，也可以在手機上「加到主畫面」。

## v3.0 新功能：個人投資股票庫存管理

- **持股輸入與維護**：用彈出視窗新增、編輯或刪除持股，欄位包含股票代號、買入單價、買入股數、買入日期和備註；沒有行情的股票可以手動輸入現價。
- **即時損益統計**：顯示總投資成本、當前總市值、未實現損益與總報酬率。手續費預設 0.1425% 打 2.8 折（最低 20 元），證交稅 0.3%（ETF 0.1%），都可以自行修改。
- **資產配置圖**：用甜甜圈圖顯示各持股的市值比重。
- **風險警示**：持股觸發 AI「減碼 / 避險」或跌破週 20MA 時，列表上會顯示紅色警戒標籤。
- **資料保存**：持股資料自動存在瀏覽器的 localStorage，重新整理或關閉網頁後仍會保留，而且**不會上傳到 GitHub**。換手機或電腦時，用「匯出」「匯入」搬移。

## 一次性設定（手機即可完成，約 5 分鐘）

> GitHub 免費帳號只有**公開（Public）**儲存庫可以使用 GitHub Pages。網頁上只有公開的市場行情；你的持股只存在你自己的瀏覽器，FinMind Token 則存在加密的 Secret 裡，都不會公開。

1. **建立儲存庫**：New repository → 名稱 `tw-semi-online` → 選 **Public** → Create。
2. **開啟網頁發布**：Settings → Pages → Build and deployment → Source 選 **GitHub Actions**。
3. **設定 FinMind Token（選填，建議設定）**：Settings → Secrets and variables → Actions → New repository secret → Name 填 `FINMIND_TOKEN`，Secret 貼上你的 FinMind Token。之後可以隨時在同一頁修改或刪除。
4. **上傳程式**：回到儲存庫首頁 → Add file → Upload files → 選 `tw-semi-online.zip`（不用解壓縮）→ Commit changes。
5. **建立自動化流程**：Actions → set up a workflow yourself → 檔名改成 `update.yml` → 清空編輯區，貼上 `update.yml` 的全部內容 → Commit changes。
6. 等待約 10–15 分鐘（首次需要回補 2 年資料）。在 Actions 分頁看到綠色勾勾後，到 Settings → Pages 就能看到網址。

建立流程後會自動完成這些事：解壓縮 zip、抓取資料、存檔並發布網頁。如果建立流程時 zip 還沒上傳，之後上傳 zip 也會自動觸發。

## 日常操作

| 想做的事 | 怎麼做 |
|---|---|
| 立即更新資料 | Actions → Update data & deploy → Run workflow |
| 新增股票 | 同上，在 `add` 欄位輸入代號，例如 `2454 2330` |
| 移除股票 | 同上，在 `remove` 欄位輸入代號 |
| 完整重抓 | 同上，勾選 `full` |
| 修改 FinMind Token | Settings → Secrets and variables → Actions → `FINMIND_TOKEN` → Update |
| 更新程式版本 | 上傳新版 zip，會自動解壓縮並重新部署 |

## 注意事項

- GitHub 的排程尖峰時段可能延遲 10–60 分鐘。所以除了 19:30，22:40 會再執行一次備援；兩次都是增量更新，不會重複抓取。
- 公開儲存庫如果連續 60 天沒有任何提交，GitHub 會暫停排程。本流程每天都會提交資料，正常使用下不會被暫停。
- 資料來源（TWSE / TPEx / MOPS / Yahoo / FinMind）若阻擋 GitHub 的海外主機，「資料驗證中心」會顯示該來源失敗，程式會自動改用其他來源互補。
- 本工具所有訊號均為規則式量化分析，僅供研究參考，不構成投資建議。
