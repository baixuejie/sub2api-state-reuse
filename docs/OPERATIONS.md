# 运行、升级与排障

对应 `local/sub2api-v1` 分支。完整脚本清单和状态规则见 [AUTOMATION.md](AUTOMATION.md)，首次配置见 [README.md](../README.md)。下文路径均为模板路径，部署时替换成自己的私有路径。

## 启动和查看状态

安装 unit 文件、填写配置、启用插件后，先运行一次调度和采集验证，再启用定时器：

```bash
sudo systemctl daemon-reload
sudo systemctl start state-scheduling.service
sudo systemctl start state-collector.service
sudo systemctl enable --now state-scheduling.timer state-collector.timer state-monitor.service

systemctl list-timers state-scheduling.timer state-collector.timer
systemctl status state-monitor.service
systemctl show state-scheduling.service state-collector.service -p Result -p ExecMainStatus
journalctl -u state-collector.service -f
```

调度与采集是 `Type=oneshot` 服务，轮次完成后显示 `inactive (dead)` 是正常现象；应同时核对 timer 是否 active、`Result=success`、`ExecMainStatus=0` 以及快照是否持续更新。

## 暂停与恢复

日常暂停可直接在主站关闭插件。自动化下轮检查会停止工作，定时器继续检查开关，监控页仍可访问。重新启用插件后自动恢复；暂停不会自动还原账号原先的调度状态。

维护源码或配置时，停止两个 timer，并等待正在运行的 service 自然结束：

```bash
sudo systemctl stop state-collector.timer state-scheduling.timer
systemctl is-active state-collector.service state-scheduling.service
```

如果仍为 active/activating，等待结束再替换脚本，避免并行运行两套采集状态。完成维护后：

```bash
sudo systemctl start state-scheduling.service
sudo systemctl start state-collector.service
sudo systemctl restart state-monitor.service
sudo systemctl start state-scheduling.timer state-collector.timer
```

停止 timer 不会自动开放已停调的账号。若要彻底解除自动调度管理，应先停止 `state-scheduling.timer`，再按备份通过管理后台恢复目标账号状态。

## 管理 API Key 重置后

采集器使用私有 `admin.env` 中的 `ADMIN_API_KEY`，不会从数据库自动同步新密钥。重置主站管理密钥后，旧配置会收到 HTTP 401，自动化暂停，但插件本体可能仍是 enabled。

用本机编辑器更新私有配置，避免将密钥写入命令历史或源码：

```bash
sudoedit /etc/sub2api-state-reuse/admin.env
sudo chmod 600 /etc/sub2api-state-reuse/admin.env
sudo systemctl start state-scheduling.service
sudo systemctl start state-collector.service
```

服务每次执行都会读取配置，无需重装插件或重启主站。确认 `automation-status.json` 恢复 `enabled=true`、采集快照继续更新。

## 升级和回退

1. 在独立工作副本修改，运行 `make check test build`；页面变化时再运行 `make test-ui`。保留原发布私钥，生成新的签名包。
2. 备份原插件包、插件配置、账号列表和调度状态、私有环境配置、脚本、systemd/Nginx 配置与票据目录。备份留在服务器私有目录。
3. 停止两个 timer，等待当前轮结束。若宿主要求停用插件才能上传，应安排短维护窗口，必要时先停调所管理账号，避免切换期间走无插件路径。
4. 上传新包，检查签名、版本、兼容性和 runtime healthy，保留原账号范围、代理和暂停设置，按原启用状态恢复插件。
5. 同步 Python/监控源码，确认两个模型分别对应自己的票据。只修改 HTML/JS 刷新即可；修改 `server.py` 需要重启 `state-monitor.service`。
6. 修改过 unit 文件需 `systemctl daemon-reload`；Nginx 配置需要先校验再重载。手动验证调度和采集后恢复两个 timer。

回退也应停止两个 timer、等待当前轮结束，恢复旧包、源码和配置，再验证并恢复服务。不要直接用旧数据库覆盖当前写入。旧版本是否支持 Sol、如何处理两种票据及冷却状态，应与采集器一起核对。

## 日志页面和网络

- 页面：与主站同域的 `/admin/state-harvest`，沿用主站管理员登录。
- 数据：`/fn-state/api`，未登录返回 401、非管理员返回 403；页面 HTML 可公开访问。
- 页面每 3 秒刷新。账号一行，Astra / Sol 分列，业务调度资格单独显示。
- `STATE_MONITOR_HOST` 默认 `127.0.0.1`。容器 Nginx 需要访问宿主监听地址；同步调整反代与防火墙，仅放行需要的私网来源。
- 采集日志与插件审计日志可用 [logrotate.conf](../deploy/logrotate.conf) 轮转；复制到 `/etc/logrotate.d/` 前，先按实际路径修改，使用 `logrotate --debug` 校验。

## 常见现象

| 现象 | 检查内容 |
| --- | --- |
| “自动化已暂停”，插件仍 enabled | 管理 API 是否 401、私有密钥是否与主站一致、主站是否可访问 |
| 新账号没有立刻入队 | `STATE_AUTO_ENROLL=true`、插件 enabled、5 秒 timer 正常；只有 OpenAI OAuth 实体账号入队 |
| 账号停调但仍有采集日志 | 正常；无票不接业务，但继续后台刷票 |
| Sol 缺票，账号仍可调度 | 正常；默认 `STATE_REQUIRED_MODELS=gpt-6-astra` |
| 返回 312 / 356，或实际模型不是目标模型 | 当前规则拒收，不改长度判定来伪装成功 |
| 票据“有效”但业务仍被当作无票 | STATE 与 Cookie 配对，捆绑约 240 秒后整体失效；确认采集器在持续续采（有效票超 150 秒即 30 秒一轮）、服务器时钟偏差不大、`tickets.json` 里的候选带 `cookies` 字段 |
| `candidate_queued`，但 `renewed=false` | 检查票据目录挂载、UID/GID、插件健康、业务账号测试是否成功 |
| 401 / 403 / 429 | 保留认证暂停和恢复时间，不删除状态或切换模型绕过冷却 |
| 上游 429 后业务请求被直接返回 429 | 插件对范围内账号启用了退避：429 保留票据并至少退避 5 分钟（尊重更长的 `Retry-After`），期间新请求直接收到带 `Retry-After` 的 429；退避仅存于插件进程内存，重启插件即清除 |
| 业务请求等待超过平时才发出 | 同一账号业务并发上限为 2，排队服从请求取消和宿主超时；宿主自身的账号限流仍独立生效 |
| 管理页面 502 | 监控服务监听地址、容器到宿主网络、防火墙和 Nginx 反代 |
| 页面状态超过 3 分钟未更新 | timer、最近 service 结果、管理凭据和采集快照时间 |
| 换动态代理后偶发网络错误 | 每次新连接是否可达；20 秒检查不代表每轮一定换 IP，复验通过才入库 |

插件请求内续采和外部定时采集是两条路径，其冷却状态不完全共享。票据、模型字段和长度是经验判定，不能保证上游之后每次响应都不降级。监控只显示最近 500 条脱敏事件，完整记录以私有日志保存策略为准。

## 不应提交的运行文件

真实环境变量、管理密钥、OAuth/代理认证、原始票据、候选、冷却游标、签名密钥、日志及部署备份都只保留在私有目录。仓库中的 `*.example.*` / `*.env.example` 是占位模板；填写后的文件不要提交。推送前同时检查当前文件与新增提交历史，`.gitignore` 不会清除已经跟踪过的敏感内容。
