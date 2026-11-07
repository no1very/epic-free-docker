# Epic 白嫖助手 · Docker 版（支持 ARM）

**当前版本：v1.1**

自动领取 Epic Games 商店每周免费游戏的容器服务。多架构镜像（**linux/arm64 + linux/amd64**），适配树莓派 4/5（64 位系统）、Oracle/AWS ARM 云主机、ARM 架构 NAS 等。

> 实现对齐活跃维护的开源项目 [claabs/epicgames-freegames-node](https://github.com/claabs/epicgames-freegames-node)（2026-11 仍活跃）：设备码授权 + `EPIC_EG1` 会话 + 结账页自动点击下单，并额外做了全自动下单（claabs 目前仅推送链接需人工点击）。

## 快速开始

```bash
# 1. 上传本目录到 ARM 主机，进入目录
docker compose build                       # 在主机上原生构建（无需 QEMU）

# 2. 启动后打开面板 http://主机IP:8321/ → 点"🔑 生成授权链接"→ 浏览器打开链接完成 Epic 登录
#    （命令行方式：docker compose run --rm epic python main.py login）

# 3. 启动常驻服务（默认每 6 小时检测领取一次）
docker compose up -d
docker compose logs -f
```

账号密码/两步验证/人机验证全部在 Epic 官方页面完成，本程序不接触你的凭据；凭据（可长期续期的 OAuth token）加密保存在 `./data/auth.json`（权限 600）。

## 命令

| 命令 | 说明 |
|---|---|
| `docker compose run --rm epic python main.py login` | 首次登录 / 重新授权（也可直接在面板点"生成授权链接"，更方便） |
| `docker compose up -d` | 常驻运行，周期自动领取 |
| `docker compose run --rm epic python main.py once` | 立即执行一次领取 |
| `docker compose run --rm epic python main.py detect` | 只看本周免费游戏（无需登录） |

## 可视化面板

常驻运行时自带 Web 面板：**http://主机IP:8321/**

- 账号在线状态、上次/下次运行时间（倒计时）
- **一键授权登录**：未登录时点"生成授权链接"，页面直接显示 Epic 官方授权链接（可点击），登录成功自动识别，无需重启
- 本周免费游戏列表（可领取 / 已领取 / 已拥有 / 失败 状态标记）
- **游戏库**：展示账号拥有的全部游戏。entitlements 全量解析——可玩授权走 catalog item 类目判断，`AUDIENCE`（受众授权）用 namespace 反查 catalog `offers` 拿真实游戏名，通常可解析出 240+ 款；内置 195 条通行中文译名（如 Saints Row → 黑道圣徒），其余保留英文原名；搜索框中英文均可搜。点"🏆 成就"展开该游戏成就列表（名称 / XP / 全服稀有度）
- 上次领取结果明细、实时运行日志
- 按钮：**立即检测领取**、**刷新免费列表**（免登录可用）、**刷新游戏库**（登录后可用，250 个 namespace 并发解析，约 15 秒）

纯标准库实现（无 CDN、无新依赖），NAS 内网直接访问。

## 配置（docker-compose.yml environment）

| 变量 | 默认 | 说明 |
|---|---|---|
| `CLAIM_INTERVAL_HOURS` | `6` | 检测频率（小时） |
| `EPIC_COUNTRY` | `US` | 账号所在地区（US/CN/HK…），影响地区黑名单过滤 |
| `EPIC_LOCALE` | `en-US` | 商店 locale |
| `WEB_PORT` | `8321` | 可视化面板端口（`0` 关闭面板；compose 已映射 `8321:8321`） |
| `PUID` / `PGID` | `1000` | 容器运行用户（NAS 上改为自己宿主机用户的，终端 `id -u` / `id -g` 查看），启动时自动修正 `./data` 属主 |
| `TZ` | `Asia/Shanghai` | 时区（日志时间） |
| `NOTIFY_METHOD` | `none` | 通知方式：`ntfy` / `bark` / `webhook` / `none` |
| `NOTIFY_URL` | — | ntfy: `https://ntfy.sh/主题`；Bark: `https://api.day.app/你的Key`；webhook: 任意接收 POST JSON 的地址 |
| `HTTP_PROXY` / `HTTPS_PROXY` | — | 可选。国内网络访问 Epic 免费列表 CDN 可能被阻断（表现为 `_ssl.c:994 handshake timed out`），指向本机代理即可，如 `http://192.168.1.2:7890` |
| `NO_PROXY` | — | 可选。配合上行，一般填 `localhost,127.0.0.1,::1,192.168.0.0/16` |

通知内容：领取成功（含游戏名）、领取失败（含原因）、需要重新登录。

## 数据目录（`./data` 卷）

- `auth.json` — OAuth 凭据（自动续期，权限 600）
- `last_run.json` — 最近一次运行结果
- `library.json` — 游戏库快照（面板"刷新游戏库"时重建）
- `logs/` — 领取失败时的结账页截图（排查用）

## 工作原理

1. **登录**（一次）：Epic 设备码授权，保存可长期滚动的 refresh token；
2. **会话**：access token 作为 `EPIC_BEARER_TOKEN` cookie 注入无头 Chromium，访问商店页换取 `EPIC_EG1` 会话；
3. **检测**：公开接口 `freeGamesPromotions` → `getMappingByPageSlug`/CMS 解析真实 offer → `getCatalogOffer` 校验 → `getOffersValidation` 过滤已拥有（GraphQL persisted query 与官方页面一致）；
4. **领取**：打开 `store/purchase` 结账页，注入 JS 自动勾选 EULA、点击下单按钮，随后 API 复核入库；EULA 自动接受、token 自动续期；
5. **调度**：容器内循环；也可 `command: python main.py once` 配合外置 cron。

## 本地开发 / 测试

```bash
python test_parser.py          # 解析逻辑自测（纯标准库，无依赖）
python main.py detect          # 实测拉取本周免费游戏
pip install playwright && playwright install chromium   # 本地跑领取流程才需要
```

## 故障排查

- **Permission denied: '/data/auth.json'**：绑定挂载目录属主与容器运行用户不一致（Docker 自动创建的挂载目录属主是 root；NAS 上属主通常是宿主机用户）。入口脚本启动时会按 `PUID/PGID` 自动 `chown` 修正，改好 `PUID/PGID` 后 `docker compose up -d --build` 即可。若存储不支持 chown（如 CIFS 共享），在宿主机手动执行 `sudo chown -R $(id -u):$(id -g) ./data`。
- **Cloudflare 质询（cf_challenge）**：部分机房/共享 IP 会对商店 GraphQL 的 entitlements 路径触发机器人质询。本项目所有权判断已改走 Epic 自家 REST 服务（`catalog-public-service` + `entitlement-public-service`，Bearer 认证，不走 Cloudflare），一般不会再遇到；公开接口（免费列表/映射/校验）不受影响。
- **领取失败排查**：`data/logs/` 下有结账页截图；自动点击失效一般是 Epic 改版，更新 `epic/claimer.py` 的 `CHECKOUT_JS` 选择器即可。
- **登录遇人机验证 / 凭据失效**：人机验证需手动通过一次；凭据过期后在面板重新"生成授权链接"即可，无需重启容器。
- **`_ssl.c:994 handshake timed out` / `UNEXPECTED_EOF_WHILE_READING`**：两条独立成因。① 免费列表 CDN 被网络阻断 → 配 `HTTPS_PROXY` 走代理；② 路径 MTU 黑洞（大包被静默丢弃，TLS 证书包超阈值即卡到超时）→ 用 `ping -M do -s 1472 IP` 二分定位，下调网卡 MTU 后再让容器继承，例如 compose 里给项目网络加 `driver_opts: {com.docker.network.driver.mtu: "1468"}`。v1.1 已给 `_http` 加了 3 次重试（1.5s / 3s 退避），单次抖动不会再毁掉整轮运行。
- **面板一直弹同一条错误**：v1.1 已修——会话/列表刷新成功时会清空 `last_error`，前端对相同错误做去重，只在错误变化时提示。

## 已知限制

- 不支持 armv7（32 位树莓派系统）；64 位系统（`uname -m` 显示 `aarch64`）均可。
- Epic 登录遇到人机验证需手动完成；凭据长期未运行失效后需重新 `login`。
- Epic 改版结账页可能使自动点击失效（`epic/claimer.py` 的 `CHECKOUT_JS` 已做多选择器 + 多语言兜底，失败会截图到 `data/logs/`，届时更新选择器即可）。
- 仅供个人学习使用，请遵守 Epic 服务条款。

## 更新日志

### v1.1

- **游戏库全量显示**：`entitlements` 里绝大部分条目是 `AUDIENCE`（受众授权，item 只返回 `SerpentineGeneralAudience` 这类内部代号）。v1.1 改为按 namespace 查 catalog `offers` 反查真实游戏名，命中率约 92%，游戏库从 1 款恢复到 240+ 款。
- **游戏库中文化**：新增 `epic/zh_names.py`（195 条通行中文译名），面板中文为主、英文原名小字显示，搜索框中英文通用。
- **并发构建**：250 个 namespace 8 路并发解析，构建耗时从一两分钟降到约 15 秒。
- **网络健壮性**：`_http` 增加 3 次指数退避重试；解决单次 SSL 抖动导致整轮构建失败。
- **面板错误提示去重**：成功时清空 `last_error`，前端仅在错误变化时弹提示（修掉"一次失败永久每 5 秒弹窗"）。
- **文档**：补充代理与 MTU 黑洞两类握手超时的排查路径。

### v1.0

首个可用版本：设备码授权、`EPIC_EG1` 会话、结账页自动下单、Web 面板、游戏库与成就查询。

