# v3.0.2 · 无需登录 GitHub 的软件更新

公开仓库版本检查和下载安装包改为直接 HTTPS 请求，无需 GitHub 账号或 GitHub CLI。继续校验 SHA256、版本清单，保留本地数据和设置。

旧版仍含登录检查，首次迁移可下载本页的 update.ps1，将它替换到原安装目录 scripts/update.ps1，然后在原目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\update.ps1 -Mode Apply
```

也可以关闭程序后，用完整新版 ZIP 覆盖原目录。升级后可直接在软件“系统与推送 → 查看更新”操作。无需删除数据或重新配置 Telegram。

公开接口仍受 GitHub 访问频率和网络限制，失败时会提示重试。本次未更改交易策略。
