# 鐳洋 BK-2 M9 過境資料儀表板

這是 TASA 實驗室 GitLab 主倉的最小化儀表板鏡像，只包含抓取、解析、建站與部署所需檔案。請勿把主倉的機密文件、簡報、韌體或照片加入此 repository。

> 資料供計畫內部使用；但此 GitHub repository 與 GitHub Pages 網站是公開的，任何人都能存取。

- 儀表板：https://vanisherzd.github.io/tasa-riot-dashboard/
- 更新排程：GitHub Actions 每小時第 7 分鐘執行，也可從 Actions 手動執行 `Deploy dashboard`。
- 本機建置：`python3 scripts/fetch_riot_passes.py --since 2026-10-01 && python3 scripts/summarize_riot_passes.py && python3 web/build_site.py`

更新鏡像時，請在 TASA 主倉執行：

```sh
sh web/deploy/sync_dashboard_repo.sh /Users/laizhendong/Desktop/tasa-riot-dashboard
```

腳本只同步白名單內的儀表板檔案與 `data/rx_logs/m9_passes/`，接著在鏡像 repo commit 並 push。
