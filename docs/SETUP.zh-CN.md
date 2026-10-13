# 首次设置

全程不要把 token、API Key、账户号、设备 ID 粘贴到聊天、Issue、提交或截图里。

## 1. 创建 Flex Query（IBKR 网页端，一次性）

IBKR Client Portal → Performance & Reports → Flex Queries → Activity Flex Query → 新建：

**主查询（必需）**

| 设置 | 值 |
|---|---|
| Sections | **Net Asset Value (NAV) in Base**：Report Date、Cash、Total |
| | **Open Positions**：Symbol、Quantity、Mark Price、Position Value、FIFO P&L Unrealized；Options 选 Summary |
| | **Change in NAV**：From Date、To Date、Mark-to-Market、Ending Value |
| Format | **XML** |
| Period | **Last 30 Calendar Days** |
| Date Format | yyyyMMdd |

保存后记下 Query ID。

**日盈亏查询（可选，想看“当日盈亏”就建）**

同样新建，Period 选 **Last Business Day**，Sections 选 **Change in NAV** 和 **Mark-to-Market Performance Summary in Base**（Symbol、Total）。不建的话屏幕上 DAY 显示 N/A，不会拿 30 天累计数冒充当日数。

## 2. 生成 Flex token

Performance & Reports → Flex Queries → Flex Web Service Configuration → 启用 → Generate New Token。

- 生成新 token 会让旧 token 失效（如果旧版看板还在用旧 token，它会停止工作——这正好避免两边同时推送）。
- token 有有效期；过期后屏幕会显示 `FLEX TOKEN EXPIRED - GENERATE A NEW ONE`。
- 可以把 token 限制为只允许本服务器的 IP 使用（更安全）。

## 3. ZECTRIX API Key

登录 [极趣云](https://cloud.zectrix.com/home/api-docs) → 开放 API → 创建 API Key。

## 4. 填配置

```bash
cd ~/projects/ibkr-zectrix-dashboard
.venv/bin/ibkr-zectrix init          # 生成 ~/.config/ibkr-zectrix-dashboard/env，权限 600
nano ~/.config/ibkr-zectrix-dashboard/env
```

填 `IBKR_FLEX_QUERY_ID`、`IBKR_FLEX_TOKEN`、`ZECTRIX_API_KEY`，可选 `IBKR_FLEX_DAILY_QUERY_ID`、`ZECTRIX_DEVICE_ID`、`ZECTRIX_PAGE_ID`。

## 5. 先检查、再推送

```bash
.venv/bin/ibkr-zectrix check --output output/check.png   # 真实取数，只渲染
.venv/bin/ibkr-zectrix devices                          # 确认设备
.venv/bin/ibkr-zectrix tick --dry-run                   # 走一遍定时逻辑，不推送
```

看过 `output/check.png` 没问题后，第一次真实推送：

```bash
.venv/bin/ibkr-zectrix tick --force
```

确认墨水屏实际刷新了（API 接受图片不等于设备已经刷新）。

## 6. 停掉旧版定时任务

在 Codex 里关闭旧插件的定时（heartbeat）任务，避免两边交替推送不同的图。

## 7. 启用定时器（需要 root，一次性）

```bash
mkdir -p -m 700 ~/.local/state/ibkr-zectrix-dashboard
sudo cp deploy/ibkr-zectrix-dashboard.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ibkr-zectrix-dashboard.timer
systemctl list-timers ibkr-zectrix-dashboard.timer
journalctl -u ibkr-zectrix-dashboard.service -n 20
```

之后用 `.venv/bin/ibkr-zectrix status` 看状态。
