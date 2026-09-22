# Sub2API STATE Reuse · v1 宿主适配

本分支基于 [little-greenbean/sub2api-state-reuse](https://github.com/little-greenbean/sub2api-state-reuse)，将 STATE 插件适配到原作者 [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api) 的 v1 插件宿主，并提供可独立部署的自动化脚本与管理员监控页。

当前插件版本：`1.0.15+sub2api.v1.6`，已在 Sub2API `0.2.7` 上验证。原项目面向另一分支的 v2 宿主；两种插件协议不能混装。本仓库保留上游 SDK、来源声明和 LGPL-3.0 许可证。

**插件和全部自动化脚本位于 [`local/sub2api-v1`](https://github.com/baixuejie/sub2api-state-reuse/tree/local/sub2api-v1) 分支。** `main` 保留原作者代码；请先切换分支再下载或开发：

```bash
git clone --branch local/sub2api-v1 https://github.com/baixuejie/sub2api-state-reuse.git
cd sub2api-state-reuse
```

文档入口：[自动化脚本与任务关系](docs/AUTOMATION.md) · [运行、升级与排障](docs/OPERATIONS.md) · [二次开发](docs/CUSTOMIZATION.md)。

## 功能

- **Astra / Sol 独立采集**：分别探测 `gpt-6-astra` 和 `gpt-5.6-sol`，按账号、模型与当前凭据隔离票据；不跨模型替换 `X-Codex-Turn-State`。
- **完整验证**：292 / 332 票据需通过结构、时间、实际响应模型与完整流校验，再携票复验后交给插件入库。长度和 HTTP 200 本身不代表成功，也不保证后续模型能力。
- **票据捆绑与 240 秒生命周期**：上游将 STATE 与会话 Cookie 配对，捆绑约 240 秒后失效。采集时同时捕获响应 `Set-Cookie`，携票复验与业务注入都回放该 Cookie；有效票超过 150 秒即按 30 秒节奏滚动续采，保证在用捆绑始终新鲜。
- **自动入队**：每 5 秒发现新的 OpenAI OAuth 实体账号，先关闭业务调度，再加入采集名单。排除影子账号、已删除账号和 synthetic UI 测试账号。
- **有票才调度**：默认只要求 Astra 有效；Sol 缺票不会单独让账号停调。无票账号继续后台采集，票据距过期不足 30 秒时提前停调。
- **插件开关联动**：插件启用时才执行入队、采集和调度同步。停用时只检查开关，保留原账号状态；重新启用后恢复并补录新账号。
- **定时采集**：每 20 秒检查；缺票账号每模型每轮尝试一个出口，同一账号的模型依次处理，全局最多 3 个账号并发。认证暂停与配额冷却跨模型共享。
- **429 防护**：范围内账号业务并发上限为 2，覆盖整个流式响应；429 保留票据并至少退避 5 分钟（尊重更长的 `Retry-After`），401/403 仅撤销请求实际使用的当前票据，旧响应不能误删新票。退避保存在单个插件进程内，重启不持久化。
- **动态轮换代理**：可选配置 HTTP 代理生成器，缺票时与 Clash、IP 管理出口交替尝试；每次选中动态出口都重新调用生成器获取新 IP。
- **管理员日志页**：一行一个账号，Astra / Sol 双列状态、有效期、最近结果、账号搜索和模型筛选，每 3 秒刷新。数据接口每次向宿主校验管理员身份。

## 页面配置代理与查看轮换

主站「插件管理 → STATE Reuse → 配置」可直接修改 HTTP(S) / SOCKS5(h) 代理地址，以及可选的动态 IP 提取接口。配置通过宿主 UI Bridge 写入原有加密插件配置，下一轮采集读取；账号和暂停名单保留。动态接口支持 HTTP(S) 路径及查询参数，需返回一行 `IP:端口`，每次采集重新取出口，携票复验使用同一次提取结果。

`/admin/state-harvest` 增加实际出口 IP、累计尝试次数与已确认换 IP 次数，按账号和模型独立持久保存。从部署本功能后开始累计，旧记录不回填。每次采集及携票复验完成后，在同一 curl 进程内使用 HTTP/1.1 访问 `https://chatgpt.com/cdn-cgi/trace`；只有后一个请求没有新建连接且返回合法公网 IP，才将该 IP 归属于前面的打票请求。探测请求不附带账号 token 或票据 Cookie。连接变化、trace 不可用或打票传输失败时显示“未确认”，不将代理入口地址当成出口。

一次 capture 算一次尝试，verify 属于同次尝试但另记请求次数。换 IP 次数统计相邻已确认地址的变化，无法确认的请求不增加轮换数；这是确认到的变化次数，不代表代理商实际完成的全部轮换。计数保存在现有 `cron-state.json`，重启服务不清零。日志指外部定时采集器的打票/复验事件，不是主站业务请求日志或插件请求内采集。

## 目录

| 目录 | 内容 |
| --- | --- |
| `plugin/` | Go 插件、v1 协议适配、签名打包器与 SDK |
| `collector/state-cron.py` | 定时采集、携票复验与候选交接 |
| `collector/scheduling.py` | 新账号入队及业务调度资格同步 |
| `collector/automation.py` | 统一插件开关与运行中取消，由采集和调度脚本调用 |
| `collector/local_ip_harvest.py` | 代理请求、模型验证、携票复验和冷却 |
| `collector/ticket_store.py` | 票据校验与按模型隔离的候选文件交接 |
| `collector/settings.py` | 统一读取部署环境配置 |
| `monitor/` | 管理员监控 API、HTML 与 JavaScript |
| `deploy/` | systemd、Nginx、日志轮转及占位配置模板 |
| `tests/` | 使用模拟数据的采集、调度、权限及界面测试 |

## 构建插件

需要 Go 1.26+、GNU Make、Python 3.10+、Node.js，以及用于 Go race 检测的 C 工具链。

```bash
make check test build
```

生成 `plugin/state-reuse-1.0.15+sub2api.v1.6.s2plugin`。首次打包会生成本地 `publisher.key` 与 `publisher.pub`；两者均不提交到仓库。升级时保留并复用私钥，避免发布身份变化。

在宿主配置中，将生成的公钥加入 `plugins.trusted_publishers`，键名对应安装包签名中的 `key_id`。当前打包器使用 `local-flownode-state-reuse-20260919`；生产环境继续保持 `plugins.allow_unsigned=false`。重启宿主加载公钥后，通过管理员插件页面上传安装包。

后台配置参考 `deploy/plugin-config.example.json`。填写自己的**容器可达**采集代理，使用自己的账号 ID，启用插件。示例账号、地址和端口都只是占位值。

## 部署自动化

示例安装路径为 `/opt/sub2api-state-reuse`，状态目录为 `/var/lib/sub2api-state-reuse`。实际路径可以调整，但需要同步修改 systemd unit。

```bash
sudo install -d -m 700 /etc/sub2api-state-reuse /var/lib/sub2api-state-reuse
sudo install -m 600 deploy/config.env.example /etc/sub2api-state-reuse/config.env
sudo install -m 600 deploy/admin.env.example /etc/sub2api-state-reuse/admin.env
```

编辑上述私有文件：

| 配置 | 说明 |
| --- | --- |
| `SUB2API_BASE_URL` | 主站管理 API，包含 `/api/v1` |
| `SUB2API_PG_CONTAINER` | 主站 PostgreSQL 容器名 |
| `STATE_PLUGIN_ID` | 上传后分配的插件数字 ID |
| `STATE_TICKET_STORE` | 与插件 `/app/data/fn-state-reuse/tickets.json` 对应的宿主挂载路径 |
| `STATE_PLUGIN_UID` / `STATE_PLUGIN_GID` | 插件进程的实际 UID/GID，供候选文件设置权限 |
| `STATE_ROOT` | 私有采集游标、脱敏事件和状态快照目录 |
| `STATE_DYNAMIC_PROXY_GENERATORS_FILE` | 可选；动态 HTTP 代理生成器的私有 JSON 配置文件，`0600` |
| `ADMIN_API_KEY` | 在宿主后台生成的管理 API Key，仅填入私有 `admin.env` |

可选的动态 HTTP 代理生成器文件使用以下格式，并应设为 `0600`。`url` 必须返回一行 `IP:PORT`；采集器每次最多读取 4 KiB，只接受合法 IP 和端口。接口失败时会跳过并继续使用 Clash/IP 管理出口。真实生成器 URL 通常包含鉴权信息，不要提交到 Git：

```json
[
  {"name": "rotating-provider", "url": "https://provider.example/generate"}
]
```

缺票时动态出口与静态出口交替尝试；每次选中动态出口都会重新调用生成器，因此连续两次动态尝试可能得到不同 IP。

v1 分支的推荐设置：

```ini
STATE_ACCOUNT_SCOPE=plugin
STATE_PROXY_SOURCE=plugin
STATE_AUTO_GROUP=false
STATE_TICKET_SCHEDULING=true
STATE_AUTO_ENROLL=true
STATE_REQUIRED_MODELS=gpt-6-astra
```

这会从插件配置读取采集代理，保留现有分组，仅通过主站正式 API 调整账号 `schedulable` 开关。代理密码留在宿主加密插件配置，不写入源码或日志。重置管理 API Key 后，需要同步更新私有 `admin.env`，否则自动化会因 401 暂停。

确保插件数据目录及 `incoming/` 存在、归属插件实际 UID/GID，且不会覆盖已有票据。然后安装任务：

```bash
sudo install -m 644 deploy/state-collector.service deploy/state-collector.timer \
  deploy/state-scheduling.service deploy/state-scheduling.timer \
  deploy/state-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start state-scheduling.service
sudo systemctl start state-collector.service
sudo systemctl enable --now state-scheduling.timer state-collector.timer state-monitor.service
```

首次手动执行会发出实际采集和复验请求并消耗账号额度。确认结果后启用 timer，日志可使用 `journalctl -u state-collector.service -f` 查看。默认使用 Docker CLI 读取账号，服务权限需与模板等价。

上传插件不会自动安装以上 systemd 服务。`state-scheduling.timer` 每 5 秒处理新账号和业务调度，`state-collector.timer` 每 20 秒检查采集需求，`state-monitor.service` 常驻提供日志页面。`automation.py` 是共享模块，不需要单独启动，三者的完整说明见 [AUTOMATION.md](docs/AUTOMATION.md)。

自动入队存在约 5 秒扫描间隔；主站导入瞬间的默认调度设置不会被脚本原子修改。若要求导入瞬间即不可调度，应在导入端先关闭调度。账号被停用、发生认证错误或额度冷却时，不会强行刷票。长期人工暂停应停用账号或加入插件 `suspended`，而不是仅关闭会被自动规则维护的 `schedulable`。

## 管理员日志页面

将 `deploy/nginx-locations.conf` 中的路径加入主站管理员页面所在的 `server {}`，可选地将 `deploy/nginx-entry.conf` 加入已有 `location /`，显示管理员右下角入口。校验后重载 Nginx。

页面路径是 `/admin/state-harvest`，日志数据路径是 `/fn-state/api`。沿用同源主站登录；HTML 壳可公开访问，数据接口匿名 401、普通用户 403。

监控服务默认监听 `127.0.0.1:17843`。若 Nginx 运行在容器中，需根据自己的网络设置 `STATE_MONITOR_HOST`，同步修改反代目标，并限制访问来源。容器里的 `127.0.0.1` 不等于宿主机；不要直接将监控端口暴露到公网。复制日志轮转模板前也应先调整里面的路径。

## 开发与验证

```bash
make check test

# 独立监控页的 DOM 测试，jsdom 可以安装到临时依赖目录。
npm install --prefix /tmp/state-ui-deps --no-save jsdom@24.1.3
NODE_PATH=/tmp/state-ui-deps/node_modules make test-ui
```

发布代码包含自动化脚本、服务模板及测试；不包含真实环境变量文件、代理凭据、管理员密钥、OAuth Token、票据、签名密钥、运行日志或服务器专用运维记录。所有 `*.example.*` 文件均为占位模板，复制后在服务器私有目录填写。

`origin` 指向本 Fork，`upstream` 指向原作者；`main` 保留上游版本，本定制开发分支为 `local/sub2api-v1`。后续同步应在独立开发分支中合并上游、解决协议适配冲突并运行测试，再更新运行中的部署。

本分支从上游 `e846350` 保留的可验证基线整理而来，并已将上游 `1950029` 的 429 防护与动态轮换代理移植到 v1 宿主；240 秒票据生命周期与 Cookie 捆绑策略参考了 [446599/ccodex-rotate](https://github.com/446599/ccodex-rotate) 的实测实现。上游后续更新仍可通过 `main` / `upstream` 获取。详细背景与运维边界见 [适配指南](docs/CUSTOMIZATION.md)、[运维说明](docs/OPERATIONS.md) 和 [NOTICE](NOTICE)。
