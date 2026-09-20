# 二次开发与适配

当前开发分支为 `local/sub2api-v1`，面向原作者 Sub2API v1 插件宿主。`main` 保留原作者版本。首次安装见 [README.md](../README.md)，脚本关系见 [AUTOMATION.md](AUTOMATION.md)。

## 修改位置

| 需求 | 位置 | 生效方式 |
| --- | --- | --- |
| 主站 API、数据库容器、插件 ID、文件路径 | 私有 `config.env`；字段定义在 `collector/settings.py` | 下轮脚本读取；监控配置变化需重启服务 |
| 管理 API Key | 私有 `admin.env` | 下轮读取，不写入 Git |
| 采集代理、账号范围、暂停账号 | 主站插件配置 `proxy_url` / `accounts` / `suspended` | 运行中应用配置，采集器下轮读取 |
| 自动入队条件、调度资格 | `collector/scheduling.py` | 等待当前调度轮结束后部署 |
| 插件启停联动 | `collector/automation.py` | 同步采集与调度脚本后验证停用/恢复场景 |
| 双模型探测、完整流和携票复验 | `collector/local_ip_harvest.py`、`collector/state-cron.py` | 等待当前采集结束后部署 |
| 候选隔离与票据格式 | `collector/ticket_store.py`、`plugin/main.go` | Python / Go 规则必须一致，重新构建插件 |
| v1 RPC / 运行时版本 | `plugin/v1_adapter.go` | 重新构建、签名并安装 |
| 清单、签名和插件包名 | `plugin/cmd/pack/main.go` | 与运行时版本一致 |
| 页面布局、筛选、文案 | `monitor/index.html`、`monitor/app.js` | 刷新页面；运行 DOM 测试 |
| 监控鉴权、数据白名单 | `monitor/server.py` | 测试权限后重启 `state-monitor.service` |
| 任务频率 | `deploy/*.timer` | 安装更新、daemon-reload 并重启对应 timer |

不要把服务器地址、代理密码、管理员密钥写进以上源码。部署值通过私有文件或主站加密插件配置提供。

## 模型与票据规则

探测清单是 `settings.MODELS` 中的 Astra 和 Sol；默认调度条件是 `STATE_REQUIRED_MODELS=gpt-6-astra`。增加模型时需要同时核对：

1. Python 请求的目标模型、完整响应判定与携票复验；不能只改显示名称。
2. Go `Forward` 是否进入该模型的候选导入和票据注入路径。
3. 候选与持久票据键中的账号、模型、凭据隔离，保留旧冷却状态。
4. 调度条件是否仍只看指定模型，以及同账号限流如何跨模型生效。
5. 监控数据、模型列和筛选，以及 Python / Go / DOM 测试。

当前只接受 292 / 332 字符票据并检查时间与实际模型，不按套餐专门分流。Team 等套餐需用真实响应单独验证；不能直接把某个长度等同于某个套餐或能力。

## 宿主协议和代理

本分支清单声明 `openai.oauth.outbound_transport.v1`，通过 `plugin/v1_adapter.go` 暴露 v1 生命周期和转发 RPC。不要把 v2 的 `openai.oauth.protection_transport.v1` 清单直接上传到 v1 宿主，也不要调用不存在的 `/admin/plugins/:id/routing`。

v1 宿主按 OAuth 类型和灰度范围将请求交给插件；插件内部对未选中账号正常转发，选中账号按对应模型注入票据。自动化通过主站正式 `schedulable` API 控制选账号阶段，不用 SQL 直接写调度状态。

`proxy_url` 用于采票，业务请求仍使用宿主传入的账号代理 `ForwardRequestStart.proxy_url`。默认采集器也从插件配置读取代理；可选 `STATE_PROXY_SOURCE=routes` 读取 Clash 路由与 IP 管理，但必须保留账号冷却规则。宿主和容器的 loopback 地址不同，应分别验证可达性。

SDK 目录保留原来源快照。适配其他主站分支时，应核对协议、管理 API、数据库只读查询字段、目录挂载与管理员角色，不要覆盖宿主生成代码来拼接协议。

## 版本与测试

当前兼容版本由 `plugin/v1_adapter.go` 的 `compatibilityVersion` 返回，打包器清单版本及产物名必须与之相同；`plugin/main.go` 的基础版本也要满足打包器检查。升级始终复用私有发布密钥。

```bash
make check test build

npm install --prefix /tmp/state-ui-deps --no-save jsdom@24.1.3
NODE_PATH=/tmp/state-ui-deps/node_modules make test-ui
```

测试覆盖模型和凭据隔离、候选导入、请求正文保留、代理认证、限流、账号停调后继续采集、自动入队顺序、插件停用时不写账号、监控权限及模型筛选。测试使用模拟凭据，不使用生产账号发请求。

扩展事件字段时同步 `local_ip_harvest.py::emit` 与 `monitor/server.py::FIELDS` 白名单；不得返回原始票据、凭据哈希、请求头或代理 URL。界面使用 `textContent` 渲染服务端文本，保持鉴权失效后的私有内容清理。

## Git 开发流程

从自己的 Fork 分支创建功能分支，完成测试后推送至自己的 `origin`。原作者远程建议命名为 `upstream`：

```bash
git switch local/sub2api-v1
git switch -c feature/my-change
# 修改、检查、提交
git push -u origin feature/my-change
```

同步上游应另建分支合并、检查 v1 适配冲突并验证；不要直接在运行目录中拉取并让未验证脚本被 timer 执行。生产切换过程见 [OPERATIONS.md](OPERATIONS.md)。
