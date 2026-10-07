# MQTT 精简读取 v1（Seenzus #2045）

这是应用与 Bridge 的可选扩展；沿用 command/result/state Topic 和现有 ACL。
仅当前连接收到的有效 online presence 中 `capabilities.snapshotStream === 1`、
`capabilities.serviceIndex === 1` 各自启用对应能力，未知版本视为不支持。
应用等待 `mqttConnected=true` 的 online presence 最多 250ms，未就绪的启动通知和 Catalog
不提前结束该等待；到期仍缺失时使用旧读取。同代次得到明确 404/501 后不再探测该能力。
Bridge 在 Catalog 发布并就绪后立即重发 retained online presence，不等待下一次心跳。
断线清除协商、缓存与在途完整性证据；撤回能力同步使对应在途读取失效，即使能力立即恢复，
旧流的迟到 State/Result 也不能完成新读取。旧聚合 Result 的完成证据仅属于对应请求，不能复用于 v1。

## 服务名称索引

请求 `GET /api/seenzus/services/index`，不改动 `GET /api/services`。
一次普通 Result，成功时 `status=200`、`success=true`：

```json
{"data":{"version":1,"isComplete":true,"count":2,"services":["light.turn_off","light.turn_on"]}}
```

名称来自 HA 服务注册表，等于完整服务描述的 domain/service 键集合；不按授权过滤，
也不因此授予控制权限。domain 与 service 均须匹配 `[a-z0-9_]+`。
应用验证版本、完整性、数量、每个名称以及唯一性；空集合须明确 count=0。
无效注册表/响应、超限、网络错误均失败，不当作空集合。只有明确 404/501 才退回完整读取。
刷新继续 single-flight，严格执行闸门继续要求当前连接的新鲜成功结果。

## 快照流

请求 `GET /api/seenzus/states/snapshot`。msgId 是请求身份，应用每次尝试生成新 ID，
在 Source 实例和连接代次内关联。此端点专用 Result 状态机；普通命令仍只等待一条 Result。
快照捕获请求开始时的 HA State 对象，范围是既有 full_snapshot 的发布范围：
排除 Bridge 自身实体及已有型号标记过滤项。范围不是 Catalog Entity 子集，也不随发送过程变动。

同一个 result/{msgId} 先发接受回执，后发结束回执；均 QoS 1、不 retain。
接受回执 `success=true,status=202,data`：

```json
{"version":1,"phase":"accepted","scope":"publishable_states","expectedCount":2,"entitiesSha256":"..."}
```

`entitiesSha256` 为排序后每个 entityId 后接 LF、拼接后 UTF-8 编码的 SHA-256 小写 hex。
Entity ID 使用 HA 的 ASCII `domain.object_id`，空集合摘要为 SHA-256(empty)。
不发送完整实体数组或属性摘要清单。

逐实体 State 沿用完整属性、原始 ts、available、eventId、`source=full_snapshot` 和
`correlationMsgId=msgId`，增加 `snapshotVersion=1`。QoS 0、不 retain。
快照是当前态证据，不生成额外真实变化；现有下游去重保持不变。

结束回执 `data` 重复相同的 version/scope/expectedCount/entitiesSha256，并包含：

```json
{"phase":"finished","outcome":"complete","sentCount":2,"omittedCount":0,"oversizedCount":0}
```

只有全部尝试发布成功才为 `success=true,status=200,outcome=complete`。
超限实体继续处理余下实体，结束为 `success=false,status=413,outcome=partial`，
遗漏量及超限量明确计数。发送失败或桥端 120 秒死线为 `status=503,outcome=failed`，
尽力发送结束回执；断线/取消可能没有结束回执，应用必须失效，不能推断成功。
接收端只有在接受与结束回执一致、唯一实体数量等于 expectedCount、集合摘要一致时完成。
接受回执、首条 State、结束回执或 PUBACK 单独都不是完整证明。
允许 State/两条 Result 乱序及相同消息重复；冲突、缺失和旧请求/旧代次消息不能证明完成。

每桥同时最多一个请求快照流，忙碌返回 409；重复同一在途 msgId 不再启动流。
完成回执在当前 MQTT 连接内保留最近 64 个请求，重复请求只重放回执、不重发实体。
支持最多 100,000 个实体，超过范围明确 413，不截断。应用整次尝试最多等待 150 秒，
v1 最多尝试三次；413/无效协议失败后停止，传输超时/暂时失败退避重试。
心跳和 Catalog 不会中断已经接受的 v1 流或并行启动另一条流。
409 忙碌会等待 150 秒后再试，重复心跳不能提前唤醒该等待。
能力撤回时清除旧 v1 的忙碌等待期限，允许单次就绪通知触发旧路径恢复，无需依赖后续心跳。
旧 GET /api/states 保留聚合 Result 和逐实体 full_snapshot；并发请求在活动快照期间返回 409。

## 报文预算与升级

所有消息沿用 Bridge 本地完整 MQTT 3.1.1 PUBLISH 字节预算：UTF-8 payload/topic、
QoS packet ID、固定头及 Remaining Length 均计入。当前 main 默认且固定 1,048,576 字节，
不推测 Broker 2 MiB；本扩展不下发预算、不改变凭据、ACL 或配对配置。
服务索引仍超限时返回现有 413 response_too_large；不截断、不新增通用分片。
日志只记录请求 ID、实体 ID、数量、大小、失败类别，不输出属性内容或凭据。

可先部署 Bridge，再部署应用；反向部署也安全：旧桥无能力声明，新应用走旧路径。
旧应用忽略新增能力，继续完整读取。回退任一端无须同时回退另一端。
应用回退时在途新流只能超时/随连接取消，不把它解释为旧请求成功。
Bridge 回退/能力撤回会使新读取失效并退回旧路径；同代次明确不支持只回退一次。
真实生产同类首次配对、重启恢复及最终发布版本的端到端验收需单独记录，单测不替代实机证据。

## 契约验证

两端保存同一份 `compact_reads_v1.json`：Bridge 的 MQTT 命令测试对照实际发布结果，
应用通过 MQTT 接收入口消费相同消息。固定例子包含中文属性、嵌套数组、原始时间和完整性摘要。
`tests/test_compact_reads_wire.py` 在真实 Paho TCP 与 TLS/WSS 上生成额外的合成抓取。
应用测试支持 `COMPACT_READ_WIRE_TRACE=<抓取 JSON>`，验证真实发送报文的跨端消费。
协议字节和性能记录见 [本地验证记录](COMPACT_READS_VALIDATION.zh-CN.md)。
