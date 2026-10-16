# 0.2.12 跨端验收记录

Bridge #69 与 App JoeWu-explorer/Seenzus#2045，执行日期 2026-10-16（Asia/Shanghai）。
使用独立 Docker 部署的真实 HA、EMQX、App Web / Worker 和 PostgreSQL；账号及实体全部为合成数据。
没有修改生产 Broker、生产凭据或用户配对。本次不是对无远程入口的用户家庭实例执行这些测试。

## 固定版本

| 项目 | 实际值 |
| --- | --- |
| Bridge | `62c0df3c3181583d5c7dd48b14cece6b141d7b2d`，0.2.12 |
| ZIP SHA256 | `e80cd51206c04b3e79594ad6166d43afcd85ec34a48c35798affa2274c705944` |
| 安装核对 | 三台 HA 的 26 个集成文件逐字节等于 ZIP；额外测试组件不属于发行包 |
| HA / Python | 2026.10.1 / 3.14.6，Linux 容器 |
| aiomqtt / Paho | 2.5.1 / 2.1.0 |
| App | `040a966d637a07a0fc62f40346a6917b5fe32ea8`，实际源码生产构建的 Web / Worker |
| 部署标识 | 本地 Web / Worker 容器完整 ID 见 [evidence.json](evidence.json) |
| Broker | EMQX 5.8.8，2 MiB（2097152 bytes） |
| 传输 | 桥连接 WSS `/mqtt`；验证证书及主机名，独立 CA 仅加入测试容器 |
| 桥预算 | HA1 / HA2 为 2048 KiB，HA3 保持默认 1024 KiB |

只读核对的生产 Web / Worker 也均为 App `040a966`，部署 ID 分别为
`4c23b69a-7b5c-47fd-8966-422d05539708` / `ecd5a775-f35e-46f3-8af0-47947c26fdb6`，状态 SUCCESS。
该元数据不表示本次测试在生产执行。模型配置使用无效测试 key 与拒绝连接的 loopback 端口；不依赖模型输出。
账号先经真实 Auth API 建立已验证账号夹具，再通过正常密码登录、会话门和地点授权配对。

## 实际结果

| 用例 | 预期与实际 |
| --- | --- |
| 全新配对 | 新 HA3 无配置项开始：发起会话 → App 登录态授权地点 → 原样访问 HA 签名回调 → HA 前端继续配置流 → entry loaded；凭据落地，真实 EMQX 认证及订阅成功 |
| 应用数据 | 三来源均 `catalogSynced=true`；每来源预置 20 个中文嵌套属性传感器，App 期望 / 实际均为 20；最终设备投影总数 75，含其他 HA 实体 |
| 完整快照 | 新模式只有 accepted / finished 元数据及逐实体 State，无聚合状态数组 Result；实际 App 确认 `completeStreamEvidence=true`；独立对照 HA REST 的完整可发布实体 ID 集合，39 / 39 一致；22 个稳定测试实体完整属性及原始更新时间一致 |
| 服务与控制 | 索引 244 项名称与完整 HA 服务 API 严格相等；正常用户会话调用实际 `/api/seenzus/devices/:id/control` 返回 202，HA 模板风扇由 off 变为 on；授权及刷新闸门保留 |
| 预算设置 | 正常 options flow 保存 2048 KiB，状态属性为 2097152；entry ID 与全部配对 data 摘要不变 |
| HA 重启 | HA1 进程重启后原配对恢复，TCP 完整读取及控制通过，client ID 稳定；HA3 随后重启后真实 App 控制入口也通过 |
| 断网恢复 | 仅 HA1 网络断开 150.89 秒，恢复后 6.03 秒重连；client ID 不变，Recorder 补回断网期间的状态；随后 WSS 完整快照及控制通过 |
| 多实例 / 稳定性 | HA2 在 13:56:58.218889–14:06:58.227761 UTC 观察 600.03 秒，41 次采样同一连接，inflight / mqueue 均为 0，recv_msg 121→235；同时 HA1 重启、断网、大报文测试没有顶掉 HA2 |
| 1–2 MiB 实体 | 约 1.12 MB 不可压缩属性，最大实际 PUBLISH 1121035 bytes；2 MiB 预算下 39 / 39 完整送达，完整属性及时间一致，后续索引及控制成功 |
| 实体超限 | 2.2 MB 属性在 HA3 的 1 MiB 预算下明确 partial：expected=40 / sent=39 / omitted=1 / oversized=1；普通实体继续，直接读取超大实体返回 413；App 没有记全量恢复，观察窗口内无重复快照请求，后续索引及控制成功 |
| 目录超限 | 受控巨型名称生成 6613784-byte Catalog，发布前拒绝；新订阅者收到 retained offline；16 秒内无额外 presence 或重复连接；修正夹具并重载后完整读取与控制恢复 |

各次快照数量随测试实体增删变化，比较采用当次集合，不将不同用例计数相加。
完整结果、版本、计数和本地驱动哈希见 [evidence.json](evidence.json)，不含 cookie、令牌、密码、配置项凭据或真实家庭属性。

## 精简读取性能

同一测试检查新旧路径的逐实体完整状态、属性、来源与时间等价。下表为真实 TCP / TLS-WSS 报文的同机合成样本观测；
内存是 Python 发布端与抓包合计峰值，不是 App 堆内存，计时不作线上性能保证。

| 样本 / 传输 | 旧 / 新总 bytes | 旧 / 新解析 ms | 旧 / 新内存 bytes | 旧 / 新总耗时 ms |
| --- | --- | --- | --- | --- |
| 100 实体 TCP | 189371 / 108641 | 0.96 / 0.31 | 619932 / 194990 | 173.79 / 177.72 |
| 100 实体 WSS | 189371 / 108641 | 0.39 / 0.47 | 636897 / 195069 | 323.20 / 244.76 |
| 2000 实体 TCP | 2118236 / 2158744 | 5.94 / 6.75 | 8833294 / 3035237 | 3485.64 / 3285.72 |
| 2000 实体 WSS | 2118236 / 2158744 | 5.93 / 7.69 | 8833302 / 3032132 | 5127.49 / 5032.84 |
| 400 服务 TCP | 407667 / 8113 | 0.72 / 0.06 | 2758094 / 75589 | 3.96 / 2.31 |
| 400 服务 WSS | 407667 / 8113 | 0.45 / 0.12 | 2807826 / 92522 | 379.24 / 9.67 |

2000 实体原聚合数组原始编码 1690000 bytes，旧路径在 1 MiB 下已返回 413，随后仍发送兼容流。
新模式此样本总 bytes 略增，收益是完整完成证明与发布端峰值降低，不能宣传为所有规模总带宽都下降。
所有实际发送包均低于所声明预算，最大单包详见 JSON。

## 自动验证与证据

- Bridge [main CI 38908922934](https://github.com/walllbf/seenzus-mqtt-bridge/actions/runs/38908922934)：HA 2025.1.4 / Python 3.13.16 与 HA 2026.10.1 / Python 3.14.5 各 444 passed，0 failed / errors / skipped；HACS / Hassfest 通过。
- 本轮 `pytest tests/test_compact_reads_wire.py`：10 passed，0 failed / skipped。
- App `npm run check` 通过；三个文件 `mqttCompactReads.test.ts`、`mqttBridge.test.ts`、`routes.pairing.test.ts`：224 passed，0 failed / skipped，消费本轮 Python 发布端实际 WSS 抓包。
- App `npm run test:db`：真实 PostgreSQL 17，生产迁移冒烟通过；schema proof 3 passed；必需 49 套件 462 passed，0 failed / skipped，119 个语义锚点全部执行。
- App [CI 38897629038](https://github.com/JoeWu-explorer/Seenzus/actions/runs/38897629038) 因账号账单 / 额度未启动，不能记通过；上述是本地证据。完整单测的 Windows 环境复核见配套 App 验收记录。

本地原始日志及驱动位于 App 工作树 `output/WH/final-acceptance/`，线级性能证据位于桥工作树 `.venv-test/final-acceptance-wire/`。
发行包内容没有因本次验收改变；保留原 tag 与 ZIP，不替换已分发的字节。

用户此前的成功加载、数据到达 App、重启和持续风扇控制正常仍按现场反馈记录；没有远程采集其版本哈希、日志或全量计数。
通用 Result 分片、巨型实体 / 目录分段、历史应用级 checkpoint 仍为原 Issue 明确排除的后续协议设计；明确 partial / 413 不代表任意规模数据均可完整交付。
