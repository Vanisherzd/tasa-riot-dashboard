# 鐳洋過境資料儀表板

`web/update.sh` 會依序下載鐳洋平台的新過境檔、重算逐場次／逐封包 CSV，再以 Python 3.9+ 標準函式庫產生可離線瀏覽的 `web/public/index.html`。頁面不載入外部 JavaScript、字型或 CDN；OpenStreetMap 只在使用者點座標連結時開啟。

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

- `web/public/index.html`：單檔儀表板，CSS 與兩張 SVG 圖均內嵌。
- `web/public/data.json`：逐場次與各 MAC 統計，供其他程式讀取。
- `web/public/*_summary.csv`、`*_packets.csv`、`files.json`：本次建站來源資料。
- `web/public/raw/*.bin`：可由場次表直接下載的原始紀錄副本。

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
