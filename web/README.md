# 鐳洋過境資料儀表板

`web/update.sh` 會依序下載鐳洋平台的新過境檔、重算逐場次／逐封包 CSV，並在已設定 `FOSSA_EXPORT_URL` 時匯出 FOSSA Nexus webhook 訊息，再以 Python 3.9+ 標準函式庫產生可離線瀏覽的 `web/public/index.html`。頁面不載入外部 JavaScript、字型或 CDN；Leaflet 1.9.4 與授權放在 `web/vendor/leaflet/`，建站時內嵌。衛星影像仍需連線至 Esri 圖磚服務；封包內容與 GPS 狀態不需網路。OpenStreetMap 只在使用者點座標連結時開啟。

## 封包內容

鐳洋與 FOSSA 的網路頁、每個裝置頁都有「封包內容」分頁；儀表板另顯示最近十包預覽。所有時間為 UTC+8，寬表格可在自己的區域內水平捲動。

- **鐳洋（BK-2 M9）**：讀取 `summarize_riot_passes.py` 產生的逐封包 CSV，依過境時間、原始檔與 UART 行號倒序排列，完整保留所有封包。每場使用原生 `<details>`，預設展開最近兩場。接收／過境時間是場次時窗，來源沒有逐包接收時刻；封包內 UnixTime 另列。每列顯示 CRC-8 結果、RSSI／SNR 與完整 raw hex。只有 CRC 通過才顯示裝置地址、座標、Unix 轉換時間與電池電壓；CRC 失敗的解碼值標示不可信並隱藏。裝置頁的 CRC 失敗封包僅依收到的地址 bytes 暫列，歸屬未驗證；地址損壞的封包仍可在網路頁找到。
- **FOSSA Nexus**：讀取本機或 Actions cache 還原的 `data/fossa/messages.jsonl`（`packet_to_record` 格式），顯示原始 message/payload、傳輸 bytes 的文字與 hex、Link／裝置名稱，以及完整 metadata（含 RSSI／SNR、衛星等可用欄位）。Base64 字串還原為 bytes；非 Base64 的原文以 UTF-8 顯示 hex 並標示傳輸編碼未確認。**廠商 payload schema 尚未提供，內容只顯示原始資料，不解碼或推測座標**；既有 GPS 定位狀態仍來自 Hub 位置回報。無紀錄時明確顯示空狀態，Hub 封包計數不代表 cache 已有封包內容。

在 TASA 主倉離線建置與檢查（不呼叫任何網路抓取腳本，也不需要帳密；公開鏡像僅同步建站所需檔案，不含檢查程式）：

```sh
python3 scripts/summarize_riot_passes.py
python3 web/build_site.py
sh web/check.sh
uvx ruff check web/build_site.py web/test_build_site.py scripts/parse_fossa.py
```

檢查會在暫存目錄建立 FOSSA 測資與輸出，驗證 HTML escaping、CRC 失敗資料遮蔽、時間與排序、裝置歸屬、空狀態及完整建站；不改動真實 messages 或提交合成資料。

## 雲端部署

`.github/workflows/dashboard.yml` 每小時第 7 分鐘抓取完整歷史資料、重建 `web/public/`，再依 repository variable `DASHBOARD_TARGET` 部署。下載的 `.bin`、CSV 與網站輸出只存在 Actions runner／artifact，不會 commit 回 repo；checkout 若已有 `data/rx_logs/m9_passes/` 歷史檔，fetch 會保留並只補下載缺少的檔案。

資料源 `https://210.61.153.16/api/lora_file_list` 是公開 HTTPS，使用 Let's Encrypt IP 憑證，GitHub-hosted runner 可直接以正常 TLS 驗證抓取，不需 VPN 或停用憑證檢查。

### Cloudflare Pages（建議）

1. 在 GitHub 建立 private repository 並 push 本 repo；若主要開發在 GitLab，可將 repository mirror 到 GitHub。
2. 到 Cloudflare Dashboard → Workers & Pages → Create → Pages → Direct Upload，先建立 Pages 專案並記下名稱。
3. 到 GitHub repository → Settings → Secrets and variables → Actions → Variables，新增：
   - `DASHBOARD_TARGET=cloudflare`
   - `CF_PAGES_PROJECT=<Cloudflare Pages 專案名稱>`
4. 在 Cloudflare 建立使用「Cloudflare Pages: Edit」模板的 API Token；回到 GitHub Actions secrets 新增：
   - `CLOUDFLARE_API_TOKEN`
   - `CLOUDFLARE_ACCOUNT_ID`
5. 到 Actions → Deploy dashboard → Run workflow 手動測試。成功後網址為 `https://<name>.pages.dev`，排程會在每小時第 7 分鐘更新。

Pages 網址預設可公開存取。若資料只限實驗室成員，建議在 Cloudflare Zero Trust → Access → Applications 建立 Self-hosted application，以 email OTP 限制登入；免費方案可供最多 50 人使用。

### GitHub Pages（備案）

private repository 使用 GitHub Pages 需要 GitHub Pro（或適用的 Team／Enterprise 方案）。設定方式：

1. 到 Settings → Pages，將 Source 設為 GitHub Actions。
2. 到 Settings → Secrets and variables → Actions → Variables，設定 `DASHBOARD_TARGET=github-pages`；Cloudflare variables/secrets 不再需要。
3. 到 Actions → Deploy dashboard → Run workflow 手動測試。
4. 成功後網址通常是 `https://<owner>.github.io/<repo>/`。

GitHub Pages 不等同存取控制；一般 Pages 網址仍是公開的。需要 email OTP 保護時，優先使用 Cloudflare Pages 加 Zero Trust Access。

### 同步獨立 GitHub 儀表板 repo

GitHub Pages 使用獨立的 `Vanisherzd/tasa-riot-dashboard` 公開鏡像，避免把 TASA 主倉的機密文件、簡報、韌體或照片推到 GitHub。資料仍供計畫內部使用，但公開 repo 與頁面 `https://vanisherzd.github.io/tasa-riot-dashboard/` 任何人都能存取。

更新鏡像時執行：

```sh
sh web/deploy/sync_dashboard_repo.sh /Users/laizhendong/Desktop/tasa-riot-dashboard
```

腳本只同步建站白名單與 `data/rx_logs/m9_passes/`，然後在鏡像 repo commit 並 push；不會 commit TASA 主倉。

### Actions 額度

每小時一次約 720 次 workflow／月；build 若約 1 分鐘，核心建置約 700–720 分鐘。private repository 的 GitHub Free 每月含 2,000 分鐘。由於 build 與 deploy 是兩個獨立 job，GitHub 會分別計時並按分鐘進位，實際用量可能接近 1,400–1,500 分鐘；可在 Settings → Billing and licensing → Usage 查看實際消耗。

## FOSSA 接入

GitHub Pages／Cloudflare Pages 都是靜態網站，不能直接接收 FOSSA POST。`web/fossa-worker/` 提供一個 Cloudflare Worker：`POST /fossa/<INGEST_TOKEN>` 接收 webhook 並寫入 KV，`GET /export/<EXPORT_TOKEN>` 供 Actions 匯出。完整指令見 [fossa-worker/README.md](fossa-worker/README.md)。

### 1. 部署 Worker

```sh
npm install -g wrangler
cd web/fossa-worker
wrangler login
wrangler kv namespace create FOSSA_KV
# 將輸出的 id 填入 wrangler.toml
wrangler secret put INGEST_TOKEN   # 給 FOSSA 的收件 token（≥16 字元，建議 openssl rand -hex 24）
wrangler secret put EXPORT_TOKEN   # 給儀表板拉取的匯出 token（另一組）
wrangler deploy
```

請用至少 32 bytes 的隨機 URL-safe token，且不要 commit。部署後會得到：

```text
Webhook：https://fossa-webhook.<subdomain>.workers.dev/fossa/<INGEST_TOKEN>
Export：https://fossa-webhook.<subdomain>.workers.dev/export/<EXPORT_TOKEN>
```

### 2. 設定 FOSSA IoT Hub

到 FOSSA IoT Hub → **Register your Device** → **Integration**，填入 Webhook URL 並選 **with metadata**。收到裝置註冊資料後，在 `data/fossa/devices.json` 補上三台 S/N 對應的 `mac`／`nodeId`。

IoT Hub 另有需 Manager 建立 API key 的 REST API，但正式路徑與回應格式尚待 FOSSA 提供。`python3 scripts/fetch_fossa.py --rest` 已保留 `FOSSA_HUB_API_BASE`、`FOSSA_HUB_API_KEY`、`FOSSA_HUB_ENDPOINT`；endpoint 空白時只提示「待 FOSSA 提供 REST 端點」，不會發送請求。

### 3. 設定 GitHub Actions

到 GitHub repository → Settings → Secrets and variables → Actions → Secrets，新增：

```text
FOSSA_EXPORT_URL=https://fossa-webhook.<subdomain>.workers.dev/export/<EXPORT_TOKEN>
```

workflow 會把 `data/fossa/messages.jsonl` 放入 Actions cache，建站前增量匯出並去重；該檔已忽略，不會 commit 到主倉或公開鏡像。

### 驗證

先模擬 FOSSA 的 with-metadata POST：

```sh
curl -fsS -X POST \
  -H 'Content-Type: application/json' \
  --data '{"mac":"01504B004800","queue_timestamp":1760091985,"sat_timestamp":1760092185,"rssi":-87,"message":"aGVsbG8gd29ybGQ=","nodeId":"67dd4ee23f647d4b31e0f8c8","source":"satellite"}' \
  'https://fossa-webhook.<subdomain>.workers.dev/fossa/<INGEST_TOKEN>'
```

應回傳 `{"ok":true}`。接著到 Actions 手動執行 **Deploy dashboard**，或使用：

```sh
gh workflow run dashboard.yml -R <owner>/<repo>
```

完成後確認 FOSSA 網路與裝置頁的「封包內容」分頁。文件範例會顯示原始 `aGVsbG8gd29ybGQ=`、`hello world` 文字及 `68656c6c6f20776f726c64` hex；不會把 payload 解讀為座標。

## 部署到 Linux 內部伺服器

以下範例把 repo 放在 `/opt/tasa`，服務帳號為 `tasa`。若路徑或帳號不同，先修改 `web/deploy/riot-dashboard.service` 與 nginx 設定。

```sh
sudo -u tasa git clone git@git.hsnl.tw:leo/tasa.git /opt/tasa
cd /opt/tasa
git config core.hooksPath .githooks
python3 --version                 # 需要 Python 3.9+
sh web/update.sh                 # 首次抓資料並產生 web/public/
```

Linux 主機需有 `flock`（通常由 `util-linux` 提供）。腳本在沒有 `flock` 的環境會改用原子目錄鎖，方便 macOS 本機驗證；正式 Linux 部署仍建議安裝 `flock`。

### 每 30 分鐘更新

```sh
sudo cp web/deploy/riot-dashboard.service web/deploy/riot-dashboard.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now riot-dashboard.timer
systemctl list-timers riot-dashboard.timer
journalctl -u riot-dashboard.service -n 100
```

更新輸出另寫入 `/opt/tasa/web/update.log`，只保留最近 500 行。手動更新同樣執行：

```sh
cd /opt/tasa && sh web/update.sh
```

### 用 nginx 提供 VPN 內瀏覽

```sh
sudo cp web/deploy/nginx.conf.example /etc/nginx/conf.d/riot-dashboard.conf
sudo nginx -t
sudo systemctl reload nginx
```

範例只允許 RFC 1918、CGNAT 與 IPv6 ULA 網段；請依實驗室 VPN 實際配置縮小 `allow` 範圍，並讓內部 DNS 的 `riot-dashboard.internal` 指向伺服器。使用者連上 VPN 後可瀏覽 `http://riot-dashboard.internal/`，或直接使用例如 `http://10.8.0.10/` 的內網位址。不要把此站直接暴露到公網。

### 最簡單替代方案

不用 nginx/systemd 也可在 repo 根目錄啟動標準函式庫 HTTP server：

```sh
python3 -m http.server 8080 --directory web/public
```

連上 VPN 後瀏覽例如 `http://10.8.0.10:8080/`。另用 `crontab -e` 加入每 30 分鐘更新：

```cron
*/30 * * * * cd /opt/tasa && sh web/update.sh
```

`http.server` 適合可信任的內網臨時使用；長期服務請用 nginx，並由 systemd 管理更新 timer。

## 產出內容

- `web/public/index.html`：單檔儀表板，CSS 與所有 SVG 圖均內嵌。
- `web/public/data.json`：逐場次與各 MAC 統計，供其他程式讀取。
- `web/public/*_summary.csv`、`*_packets.csv`、`files.json`：本次建站來源資料。
- `web/public/raw/*.bin`：可由場次表直接下載的原始紀錄副本。
- `data/fossa/messages.jsonl`：FOSSA 原始 export 歷史，只留在本機／Actions cache，不進 git。

`web/public/` 與 `web/update.log` 都是可重建產物，已加入 `.gitignore`。

## 故障排除

### 廠商主機連不到

先在伺服器測試 `curl -I https://210.61.153.16/api/lora_file_list`。若逾時，檢查伺服器對外路由、防火牆與廠商主機狀態；`web/update.log` 會保留 Python `urllib` 的實際錯誤。下載失敗時腳本會非零退出，不會繼續用不完整資料重建網站。

### TLS 憑證錯誤

下載使用正常 TLS 驗證，不應加 `-k` 或停用 Python 憑證檢查。確認主機時間正確，並更新作業系統 CA 憑證（Debian/Ubuntu 可執行 `sudo update-ca-certificates`）。若憑證的 IP/SAN 不符，請向廠商確認憑證，不要繞過驗證。

### CSV 為空或頁面沒有封包

檢查 `data/rx_logs/m9_passes/files.json` 是否有場次、對應 `.bin` 是否已下載，再手動執行：

```sh
python3 scripts/summarize_riot_passes.py
python3 web/build_site.py
```

場次存在但封包為 0，可能是該 `.bin` 沒有可解碼的 `CC` 且長度至少 19 bytes 的封包；可用 `python3 scripts/parse_riot_log.py data/rx_logs/m9_passes/<file>.bin` 檢查。若 `files.json` 本身為空，先確認抓取日期與平台清單，再重跑 `sh web/update.sh`。
