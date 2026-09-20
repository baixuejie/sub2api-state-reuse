# 应该怎么改

本 Fork 的 `local/sub2api-v1` 分支面向原作者 Sub2API v1 插件宿主，增加了 `plugin/v1_adapter.go`、双模型采集、自动入队和账号调度同步。下面保留的 v2 宿主适配背景来自原项目；本分支实际安装步骤及开关以根目录 [README.md](../README.md) 为准。不要向 v1 宿主调用 `/admin/plugins/:id/routing`，当前通过账号 `schedulable` API 控制调度。

## 优先改配置，不改源码

先把 `deploy/config.env.example` 复制到服务器 `/etc/sub2api-state-reuse/config.env`。服务器地址不写在源代码里；SSH 使用你自己的别名，代理凭据来自号池 IP 管理，Clash 原始订阅留在你自己的本机。

| 你要改的内容 | 修改位置 | 生效方法 |
|---|---|---|
| 号池端口、PostgreSQL容器、插件ID、目标分组 | `config.env` | 下一轮采集读取；监控的API地址改动需重启monitor |
| 票据宿主目录、容器UID/GID | `config.env` 和 Docker bind mount | 保证宿主与插件读同一份文件，先停定时器再调整 |
| Clash节点与监听端口 | 私有mihomo配置、SSH `-R`、`routes.json` | 三者端口一致；重启核心/SSH，下一轮读取路由 |
| 日志页面端口 | `STATE_MONITOR_PORT` 与 nginx upstream | 重启monitor、`nginx -t` 后 reload |
| 管理员凭据 | `admin.env` | 下一轮登录读取，不写前端 |
| 插件采集代理 | 后台插件配置 `proxy_url` | 该地址必须从容器可达 |
| 页面文字/颜色/筛选 | `monitor/index.html`、`app.js` | 同步文件后刷新，服务每次直接读取文件 |

## 代理与容器网络

宿主采集用 `127.0.0.1:18300`。插件在容器内，需要额外桥接。不要把一个仅监听宿主loopback的代理地址直接填进容器。

一种可选实现：

```ini
# /etc/systemd/system/state-proxy.socket
[Unit]
Description=Clash access for the Sub2API container network
[Socket]
# 示例Docker网关；替换成你实际容器能到达的宿主地址。
ListenStream=172.18.0.1:17892
[Install]
WantedBy=sockets.target
```

```ini
# /etc/systemd/system/state-proxy.service
[Unit]
Description=Forward container proxy connections to the SSH loopback port
Requires=state-proxy.socket
After=state-proxy.socket
[Service]
# 可用 command -v / find 核对你系统的 systemd-socket-proxyd 安装路径。
ExecStart=/usr/lib/systemd/systemd-socket-proxyd 127.0.0.1:18300
NoNewPrivileges=true
```

确认 Docker 网关、绑定地址存在、目标 SSH 端口在线后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now state-proxy.socket
```

此示例插件 `proxy_url` 为 `http://172.18.0.1:17892`；不要照抄 `plugin-config.example.json` 的 `host.docker.internal`，除非你已经将该主机名映射到这个可达地址。监听绑定在专用Docker网关，并用防火墙限制来源；不要监听公网0.0.0.0。

代理分两类：`proxy_url` 仅用于插件采票；业务流量使用宿主传入 `ForwardRequestStart.proxy_url` 的账号代理。它们不能混用。开启“系统HTTP_PROXY”也不能替代账号业务代理的明确配置。

## 采集规则

本分支探测 `gpt-6-astra` 和 `gpt-5.6-sol`，默认仅 Astra 票据影响账号调度。292/332 含义及实际响应模型是经验判断，不是能力评分。

| 规则 | 代码位置 |
|---|---|
| 接受292/332、时间戳与过期 | `collector/ticket_store.py::candidate`、`state-cron.py::valid`、`plugin/main.go::parseState/validTicket` |
| 实际模型与完整流 | `collector/local_ip_harvest.py::request/collect`、`plugin/main.go::harvest` |
| 携票第二次请求 | `local_ip_harvest.py::collect` |
| 每轮1出口、失败后轮换 | `order_routes` |
| 缺票20秒、有效票续采300秒间隔 | `local_ip_harvest.py::retry_interval` 与 `collect` 的 `next_attempt` |
| 全局3并发、150秒采集预算 | `state-cron.py` 的 ThreadPoolExecutor/deadline |
| 50分钟开始续票、3570秒视为过期 | Python valid/调度与Go validTicket/ticket；改动需同步 |
| 每20秒调度、270秒进程上限 | `deploy/state-collector.timer/service` |
| 账号组切换 | `state-cron.py::sync_groups` |

如果增加一个模型，不能只改 `settings.MODEL`：Go插件对Astra的自动采集分支、前端实际模型校验、SQL中的model mapping、票据key隔离、测试和页面文字都要同步。**模型名和票据长度是两种不同维度**，不要为了让UI变绿而只修改显示判断。

采集器401/403会按当前凭据哈希暂停，429按上游恢复时间等待。不要通过删除cron-state或轮换IP跳过这些暂停。更换合法OAuth凭据后会使用新的独立状态。

## 适配其他Sub2API分支

本仓库没有携带整个主站源码，也没有通用数据库迁移。检查目标分支以下链路：

1. **插件宿主**：管理页/接口能安装、启用v2保护传输插件，清单能力被识别。
2. **业务转发**：账号代理经 `ForwardRequestStart.proxy_url` 传给插件；当前proto对应 `plugin/pkg/pluginapi/v1/plugin.proto`。若宿主是旧proto，应在宿主统一生成代码，而不是手工修改一个字段编号。
3. **管理API**：`collector/state-cron.py::call/login`、`monitor/server.py::Handler.do_GET` 中的URL、返回结构和admin角色。
4. **SQL**：`state-cron.py::run_locked/sync_groups` 和 `local_ip_harvest.py::routes_for`。重点核对 credentials JSON字段、逻辑删除、账号可调度条件和代理过期字段。只读查询取数据，分组写入走管理API。
5. **目录**：插件固定写 `/app/data/fn-state-reuse`，采集器的 `STATE_TICKET_STORE` 必须是其宿主路径；`incoming`由插件在锁内验证并消费。
6. **前端登录**：当前前端读取 `auth_token` / `auth_user`，如果宿主变更了存储方式，调整app.js/entry.js；服务端仍必须独立鉴权。

不要把 `plugin/pkg/pluginapi` 的快照覆盖进一个不同版本宿主然后直接上线；先隔离环境测试协议匹配。

## 插件二次开发

`plugin/main.go` 的版本和 `plugin/cmd/pack/main.go` 打包版本必须一致。打包器会检查runtime版本声明，避免宿主以“清单与运行时不一致”拒绝加载。

当前公开版本1.0.12对生产派生版本1.0.11的适配差异：

- 不再把 `proxy_url` 固定为某个生产Docker网关；显式要求提供有效HTTP/HTTPS/SOCKS5/SOCKS5H代理。
- Python的目录、管理API、容器名、插件ID、分组与UID/GID集中在环境配置。
- 未配置Clash路由文件时允许只使用IP管理代理。
- Go业务转发测试使用独立client mock，不会请求真实上游。
- 去掉历史一次性部署脚本、生产ID列表、备份、二进制与私钥。

修改后执行：

```bash
make check test build
```

Go测试包括292/332校验、候选导入持久化、过期与凭据隔离、完整流末尾失败、并发长正文、代理认证和普通转发。Python测试包括限流停止、认证暂停、轮换边界、复验失败不入库、候选文件权限、日志白名单及管理员权限。

## 前端展示

默认通过nginx额外路由托管只读页面，再以同源JS添加管理员入口。没有重建宿主Vue应用。

要做成原生侧栏：在宿主router增加管理员路由，新增页面组件，将 `monitor/app.js` 的 `/fn-state/api` 请求和展示逻辑移入组件；沿用宿主auth store与会话刷新。侧栏条目按admin角色显示，数据API仍逐次验证角色。移除可选nginx-entry注入以免重复入口。

事件与状态字段：

- `attempt_started` / `attempt_finished`：单次请求，`phase=capture|verify`。
- `candidate_queued`：携票复验通过并写incoming，尚不等于插件已消费。
- `account_finished`：`renewed=true`才表示本轮已从持久化存储核验到新票据。
- `paused`：冷却或认证暂停。
- `cron-status.json`：一轮结束后的快照；进行中的尝试通过事件流每3秒更新。

扩展日志字段时，要同步采集器 `emit` 白名单和监控 `FIELDS`。不能直接返回原始响应、请求header、代理URL、完整配置或ticket value。
