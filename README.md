# SeenzusAI MQTT Bridge (HAOS Plugin)

SeenzusAI MQTT Bridge 运行在 Home Assistant 本地，通过公网 MQTT 实现云端与局域网 HA 的双向联通，无需内网穿透。

主要特性：

- 按 `bridgeId` 隔离 Topic
- 命令 / 结果 / 状态 三通道
- 状态回显以事件流为主
- 支持快速配对和手动 MQTT 桥接配置
- 配置页改为单页折叠分组，保存后自动重载
- 过滤桥自身诊断实体，避免 `state` 自激循环
- 当 `bridgeId` / `topicRoot` 变化时清理旧桥 retained `presence`

---

## 架构概览

```text
Client/Cloud
  ├─ publish command
  ├─ subscribe result
  └─ subscribe state
         │
         ▼
MQTT Broker
         │
         ▼
SeenzusAI MQTT Bridge (HAOS)
  ├─ 执行 HA 内部 API（无需 HA Token）
  ├─ 回发 result
  └─ 推送 state/presence
```

---

## 安装

### HACS（推荐）

1. HA -> HACS -> Integrations
2. 添加自定义仓库（类型 Integration）
3. 安装 `SeenzusAI MQTT Bridge`
4. 重启 Home Assistant

### 手动安装

将 `seenzus_bridge` 目录复制到：

```text
config/custom_components/seenzus_bridge/
```

然后重启 HA。

---

## 配置项

在 HA -> 设置 -> 设备与服务 -> 添加集成 -> `SeenzusAI MQTT Bridge`。

当前配置页行为：

- 第一步先选择 `快速配对（推荐）` 或 `手动配置（高级）`
- 第二步进入对应模式的专属表单
- 快速配对页只保留 `Seenzus API 地址`，随后跳转外部 Seenzus 页面完成授权
- 外部授权成功后，浏览器会直接回跳到 HA 本地 callback，由插件自动兑换 MQTT 桥接配置并创建 entry
- 手动配置页仍保留 MQTT 连接参数、手动配对参数和高级参数
- 保存后会自动重载集成，配置立即生效，无需手动重启 HA

| 配置项 | 说明 | 默认值 |
|---|---|---|
| 配对模式 | `seamless` / `manual` | `seamless` |
| Seenzus API 地址（快速配对） | 创建外部配对会话并自动回写桥接配置 | 空 |
| MQTT Broker 地址 | 手动配置时填写的公网 MQTT 地址 | - |
| MQTT 端口 | Broker 端口 | `1883` |
| MQTT 用户名/密码 | Broker 认证 | 空 |
| V2 Topic 根路径 | v2 协议根路径 | `savant/v2` |
| Bridge ID | 留空自动生成稳定 ID | 自动 |
| 启用实体状态事件推送 | 推送 `state` 通道 | `true` |
| Seenzus API 地址 | 手动 MQTT 桥接配置时不使用 | 空 |



---

## MQTT Topic 规范（v2）

设：

- `topicRoot = savant/v2`
- `bridgeId = ha-xxxx`

则：

- 命令订阅：`{topicRoot}/bridge/{bridgeId}/command/+`
- 结果发布：`{topicRoot}/bridge/{bridgeId}/result/{msgId}`
- 状态发布：`{topicRoot}/bridge/{bridgeId}/state/{entityId}`
- 在线心跳：`{topicRoot}/bridge/{bridgeId}/presence`（retain）

### command 示例

Topic:

```text
savant/v2/bridge/ha-demo/command/550e8400-e29b-41d4-a716-446655440000
```

Payload:

```json
{
  "msgId": "550e8400-e29b-41d4-a716-446655440000",
  "method": "POST",
  "path": "/api/services/light/turn_on",
  "body": {
    "entity_id": "light.living_room",
    "brightness": 180
  }
}
```

### result 示例

Topic:

```text
savant/v2/bridge/ha-demo/result/550e8400-e29b-41d4-a716-446655440000
```

Payload:

```json
{
  "msgId": "550e8400-e29b-41d4-a716-446655440000",
  "bridgeId": "ha-demo",
  "success": true,
  "status": 200,
  "data": [],
  "finishedAt": "2026-04-14T10:20:30.123456"
}
```

### state 示例

Topic:

```text
savant/v2/bridge/ha-demo/state/light.living_room
```

Payload:

```json
{
  "eventId": "2c188bfd-c947-4d2e-9a70-2b72464b88b2",
  "bridgeId": "ha-demo",
  "entityId": "light.living_room",
  "state": "on",
  "attributes": {
    "brightness": 180
  },
  "ts": "2026-04-14T10:20:30.456789",
  "source": "ha_state_changed",
  "correlationMsgId": "550e8400-e29b-41d4-a716-446655440000"
}
```

---

## 状态回显语义

推荐客户端按以下优先级处理：

1. `result`：判断命令是否执行成功
2. `state`：作为最终设备状态依据
3. 若未及时收到 `state`，再主动拉取一次状态兜底

补充说明：

- `state` 是事件流，不是快照缓存；默认不使用 retain
- 插件会过滤自己创建的诊断实体（如 `sensor.seenzus_bridge_*`），不会再把这些内部状态镜像到 MQTT
- 插件会过滤名称（friendly_name）中带星号 `*` 的实体（部分集成用 `*` 标注型号/变体），既不上报 `state` 也不进入 `catalog`
- 通过 MQTT 命令触发的状态回显，`source` 可能为 `command`
- 通过 HA 内部真实状态变化触发的状态事件，`source` 可能为 `ha_state_changed`

---

## 配对模式

插件当前支持两种配对模式：

### 1. 快速配对（推荐）

配置流行为：

- `pairing_mode = seamless`


配置页执行链路：

```text
输入 Seenzus API 地址
  -> 创建带 redirect_uri/state 的 web pairing session
  -> 跳转外部 Seenzus 页面
  -> 用户完成授权
  -> Seenzus 后端 302 回跳到 HA callback
  -> 插件完成 state 校验与 code exchange
  -> 自动拿到 mqtt.host / mqtt.port / mqtt.username / mqtt.password / topicRoot / bridgeId
  -> 创建 entry
```

运行态执行链路：

```text
entry 已包含 web_pair 写入的 mqtt + bridge 绑定上下文
  -> MQTT 建桥启动
  -> 直接进入可用状态
```

成功后可在传感器属性中看到：

- `pairing_mode=seamless`
- `pairing_status=bound`
- `pairing_session_id`
- `pairing_expires_at`
- `verification_code`
- `pairing_bound_at`
- `config_source=web_pair`
- `pairing_last_step`
- `pairing_last_api_base`

### 2. 手动 MQTT 桥接（高级）

手动模式只需要配置 MQTT 桥接参数，插件连接 MQTT 成功后直接进入可用状态。









成功后可在传感器属性中看到：

- `pairing_mode=manual`
- `pairing_status=bound`
- `pairing_session_id`
- `verification_code`
- `pairing_last_step`

---

## 监控与排障

插件会创建传感器：`SeenzusAI MQTT Bridge 状态`，包含：

- `request_count`, `result_count`, `state_push_count`, `error_count`
- `topic_root`, `bridge_id`
- `pairing_mode`, `pairing_status`, `pairing_session_id`
- `pairing_expires_at`, `verification_code`, `pairing_bound_at`
- `config_source`
- `pairing_last_step`, `pairing_last_api_base`
- `last_error`

配对接口调用可观察性：

- 快速配对会记录创建 web session、外部授权完成、MQTT 配置落地、bridge 启动等步骤日志
- 也可以直接在 `SeenzusAI MQTT Bridge 配对状态` 实体属性里查看 `pairing_last_step` 和 `pairing_last_api_base`

关于 `presence`：

- `presence` 使用 retain，只保留同一 topic 的最后一条消息
- 保存配置会自动 reload，因此同一桥通常会看到一次 `offline -> online`
- 如果你修改了 `bridgeId` 或 `topicRoot`，插件会在 reload 前删除旧桥 retained `presence`，避免旧桥残留

日志路径：

- HA -> 设置 -> 系统 -> 日志
- 搜索 `seenzus_bridge`

---

## 支持的 HA 内部 API 映射

- `GET /api`
- `GET /api/config`
- `GET /api/states`
- `GET /api/states/{entity_id}`
- `POST /api/services/{domain}/{service}`
- `POST /api/events/{event_type}`
- `POST /api/template`

---

## 运行要求

- Home Assistant 2026.3+
- Python 3.11+（HA 内置）
- 公网 MQTT Broker（推荐 EMQX Cloud / HiveMQ）

---

## 测试与验证

仓库已补充隔离测试环境与行为测试，当前覆盖重点包括：

- 内部诊断实体不会被重复镜像到 `state`
- 普通实体状态变化会被发布到正确的 `state` topic
- 配置变更触发 reload 前会清理旧桥 retained `presence`
- reload 流程会在清理后继续调用配置项重载

推荐在仓库根目录执行：

```text
python -m venv .venv-test
.\.venv-test\Scripts\python -m pip install -r requirements_test.txt
```

运行测试：

```text
.\.venv-test\Scripts\python -m pytest tests test_bridge_retained_topics.py test_entity_filters.py -q
```

测试覆盖对照表见：

```text
docs/test-coverage-matrix.md
```

无感配对设计文档见：

```text
docs/seamless-pairing-design.zh-CN.md
```

无感配对插件实施任务清单见：

```text
docs/seamless-pairing-implementation-plan.zh-CN.md
```

无感配对后端接口协议建议见：

```text
docs/quick-pair-flow.zh-CN.md
```

---

## 版本变更记录

### v0.1.3 (2026-06-24) — 首个 HACS 公开发布

- 快速配对从“外部页 + 轮询 session 状态”改为“外部页授权成功后直接回跳 HA callback + code exchange 自动收尾”
- 后端 `web-pairing/session` 新增 `redirectUri/state` 契约，`complete` 支持 302 回跳，新增 callback code exchange 接口
- 插件新增回跳状态校验、授权失败/超时/兑换失败错误提示，并补齐 callback 测试覆盖

> 以下为公开发布前的内部迭代记录(内部版本号,仅供参考)。

### v3.0.8

- 快速配对改为“外部授权 + callback code exchange 自动回写 MQTT”流程
- 快速配对页不再展示 MQTT 与高级参数，手动配置完全独立
- quick pair 运行态新增 `config_source`，并补充 `waiting_external_auth / mqtt_config_received / bridge_starting / bridge_ready` 相关步骤可观察性
- 插件测试总数更新为 `57`

### v3.0.7

- 配置页改为真正的“两段式模式选择”，先选模式再进入对应表单
- 快速配对支持本地 `http://IP:port` 形式的 Seenzus API 地址
- 配对状态实体与日志新增 `pairing_last_step / pairing_last_api_base`

### v3.0.6

- 移除旧无感配对链路，只保留 web-pairing 快速配对
- 配置页改为“两段式模式选择”，先选快速配对/手动配置，再进入对应表单
- 配对状态传感器新增 `pairing_mode / pairing_expires_at / pairing_bound_at`

### v3.0.5

- 配置页改为单页折叠分组，保存后自动重载集成
- 过滤桥自身诊断实体，避免 `state` 自激循环
- 当 `bridgeId` / `topicRoot` 变化时，自动清理旧桥 retained `presence`

### v3.0.4

- 修复桥自身诊断传感器被重复镜像到 `state` 的问题

### v3.0.3

- 修复状态事件监听导致的 MQTT 发布回路问题

### v3.0.2

- 引入单页折叠配置
- 配置保存后自动 reload，使 `enable_state_events` 等参数立即生效
