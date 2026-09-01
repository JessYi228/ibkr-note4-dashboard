# IBKR ZECTRIX Dashboard 插件

[English](README.md)

这是一个可公开发布、只读的插件：把 Interactive Brokers 投资组合渲染成 400 x 300 黑白仪表盘，并可将用户确认过的图片发送到 ZECTRIX NOTE4。本项目为独立开发，与 Interactive Brokers 或 ZECTRIX 不存在隶属、背书或赞助关系。

插件会记住非敏感选项，后续运行不需要反复配置；真正的凭据始终保存在每位用户自己选择和控制的位置。

## 插件会记住什么

偏好文件只保存：

- 数据源；
- 密钥存储方式；
- 时区和币种；
- ZECTRIX 页面编号；
- 最多显示的持仓数量。

它绝不会保存 IBKR 或 ZECTRIX 凭据、账户 ID、设备 ID、密码或原始投资组合响应。默认位置为 `~/.config/ibkr-zectrix-dashboard/preferences.json`；在 POSIX 系统上文件权限固定为 `0600`。

## 密钥保存方式

| 选项 | 适用场景 | 密钥实际保存位置 |
| :--- | :--- | :--- |
| `keychain` | 用户自己的 Mac | 该用户的 macOS Keychain |
| `environment` | VPS、容器、CI 或云端 | 由受保护文件或 Secret Manager 注入的运行时环境变量 |
| `auto` | 既使用本机 Keychain，也可能临时注入环境变量的 Mac | 优先读取环境变量，再读取 macOS Keychain |

`environment` 可以配合权限为 `0600` 的服务环境文件、GitHub Actions Secrets、Docker/Kubernetes Secrets、AWS Secrets Manager/Parameter Store、Google Secret Manager、Azure Key Vault 等使用。插件只记住你选择了 `environment`，不会保存云平台凭据或密钥值。

## 首次配置

不要把 API key 或 token 粘贴到聊天里。让插件询问数据源和密钥保存方式，然后只保存这些非敏感选项：

渲染器要求 Python 3.11 或更高版本，以及 Pillow 9.4–11.x。先运行 `python3 -c "import PIL; print(PIL.__version__)"` 检查；若缺失，只能在获得用户授权后按 `skills/ibkr-zectrix-dashboard/requirements.txt` 安装，定时任务不得自行安装依赖。

```bash
python3 scripts/ibkr_zectrix_dashboard.py configure \
  --source codex_ibkr \
  --secret-backend keychain \
  --timezone America/New_York \
  --currency USD
```

命令会显示安全的下一步：Keychain 模式让用户在自己的终端提示中无回显输入；环境变量模式只列出部署平台需要注入的变量名。

随时可以查看已记住的选项和脱敏后的凭据就绪状态：

```bash
python3 scripts/ibkr_zectrix_dashboard.py settings --json
```

运行时环境变量优先于已保存的非敏感偏好。只有用户主动修改选项时才需要重新运行 `configure`。

## 数据源

- `codex_ibkr`：从另行连接且只读的 IBKR 插件获取当前数据；该连接没有安装或未认证时不可用。
- `flex`：适合无人值守的报表数据，通常为 T-1，而不是盘中实时。
- `client_portal`：用户本地 Gateway 会话已认证时可接近实时。
- `json`：用于开发或自定义适配器的明确脱敏输入。

## 安全验证顺序

```bash
python3 scripts/ibkr_zectrix_dashboard.py settings --json
python3 scripts/ibkr_zectrix_dashboard.py preview \
  --input assets/sample_snapshot.json \
  --output output/preview.png
python3 scripts/ibkr_zectrix_dashboard.py devices
python3 scripts/ibkr_zectrix_dashboard.py run --no-push
```

首次读取已连接的 IBKR 数据或向 ZECTRIX 实时推送前，请运行 `python3 scripts/ibkr_zectrix_dashboard.py authorize`。插件只询问一次，并在私有 preferences 文件中保存一个不含秘密的布尔授权值。自动化使用 `authorize --check` 验证；可用 `authorize --revoke` 撤销。

第一次真实推送前必须检查图片。定时运行可以复用已经记住的选项，但认证或读取失败时必须停止，不能改用样例或旧数据。

数据处理规则见 [PRIVACY.md](PRIVACY.md)，使用条款见 [TERMS.md](TERMS.md)，支持入口为 [GitHub Issues](https://github.com/JessYi228/ibkr-note4-dashboard/issues)。本插件只用于信息展示，绝不会下单、改单或撤单。
