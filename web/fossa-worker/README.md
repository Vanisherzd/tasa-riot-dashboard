# FOSSA Nexus webhook Worker

這個 Cloudflare Worker 提供 FOSSA IoT Hub 可呼叫的 HTTPS webhook，並把每筆資料保存在 Workers KV。Token 同時保護寫入與匯出端點；請使用至少 32 bytes 的隨機 URL-safe 字串，且不要把 token 寫入 repo。

## 部署

```sh
npm install -g wrangler
cd web/fossa-worker
wrangler login
wrangler kv namespace create FOSSA_KV
```

把指令輸出的 namespace `id` 填入 `wrangler.toml` 的 `[[kv_namespaces]]`，再設定 secret 並部署：

```sh
wrangler secret put WEBHOOK_TOKEN
wrangler deploy
```

部署後端點為：

```text
https://fossa-webhook.<subdomain>.workers.dev/fossa/<TOKEN>
```

到 FOSSA IoT Hub → **Register your Device** → **Integration**，填入上述端點並選擇 **with metadata**。儀表板使用的讀取網址是：

```text
https://fossa-webhook.<subdomain>.workers.dev/export/<TOKEN>
```

健康檢查不需要 token：

```sh
curl https://fossa-webhook.<subdomain>.workers.dev/health
```

Cloudflare 免費方案目前提供 Workers 每日約 100,000 次請求與 KV 1 GB 儲存空間，足以負擔少量衛星裝置 webhook；實際限制仍以 Cloudflare Dashboard 顯示的當期方案為準。
