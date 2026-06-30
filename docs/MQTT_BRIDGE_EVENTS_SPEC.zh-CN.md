# MQTT 桥接事件对接文档

本文档面向后端、前端和联调同学，说明当前 `seenzusaimqttbridge` 插件在 MQTT 桥接层使用的事件：

- `command`
- `result`
- `state`
- `catalog`
- `presence`

本文档以当前插件实现为准。

## 1. 通用约定

默认约定：

- `topicRoot = seenzus/v2`
- `bridgeId = ha-xxxx`

topic 模板如下：

- `command`: `{topicRoot}/bridge/{bridgeId}/command/{msgId}`
- `result`: `{topicRoot}/bridge/{bridgeId}/result/{msgId}`
- `state`: `{topicRoot}/bridge/{bridgeId}/state/{entityId}`
- `catalog`: `{topicRoot}/bridge/{bridgeId}/catalog`
- `presence`: `{topicRoot}/bridge/{bridgeId}/presence`

补充说明：

- `topicRoot` 会去掉首尾 `/`，空值回退为 `seenzus/v2`
- `bridgeId` 来自配置；如果未配置，插件使用 `ha-{entry_id前12位}`；配置值会转小写，并把非法字符替换成 `-`
- 插件订阅 command 时使用 `{topicRoot}/bridge/{bridgeId}/command/+`
- 当前实现里 `presence` 和 `catalog` 使用 `retain=true`
- 当 `bridgeId` 或 `topicRoot` 变化时，插件当前只主动清理旧 `presence` retained 消息；旧 `catalog` retained 消息不会主动清理
- `state` 是事件流，不是 retained 快照
- 如果 topic 中的 `entityId` 包含 `/`，插件会替换成 `_`
- 时间字段统一使用 UTC ISO 8601，示例：`2026-04-14T10:20:30.123456+00:00`

## 2. command

### 2.1 作用

后端/云端通过 `command` 向 HA 插件下发控制指令。

### 2.2 方向

后端/云端 -> 插件

### 2.3 Topic

```text
{topicRoot}/bridge/{bridgeId}/command/{msgId}
```

示例：

```text
seenzus/v2/bridge/ha-demo/command/550e8400-e29b-41d4-a716-446655440000
```

### 2.4 Payload

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

### 2.5 字段说明

- `msgId`: 请求 ID，建议与 topic 中的 `msgId` 一致
- `method`: HA 内部 API 方法，如 `GET`、`POST`
- `path`: HA 内部 API 路径
- `body`: 可选，请求体

### 2.6 后端要求

- 必须保证 `msgId` 可追踪、可关联
- 建议把每次 `command` 视为一次独立请求事务
- 发送 `command` 后，应等待对应的 `result`
- 如果是控制类命令，后端还应继续订阅 `state`，用于确认设备最终状态

## 3. result

### 3.1 作用

插件对 `command` 的执行结果回执。

### 3.2 方向

插件 -> 后端/云端

### 3.3 Topic

```text
{topicRoot}/bridge/{bridgeId}/result/{msgId}
```

示例：

```text
seenzus/v2/bridge/ha-demo/result/550e8400-e29b-41d4-a716-446655440000
```

### 3.4 Payload

成功示例：

```json
{
  "msgId": "550e8400-e29b-41d4-a716-446655440000",
  "bridgeId": "ha-demo",
  "success": true,
  "status": 200,
  "data": [],
  "finishedAt": "2026-04-14T10:20:30.123456+00:00"
}
```

失败示例：

```json
{
  "msgId": "550e8400-e29b-41d4-a716-446655440000",
  "bridgeId": "ha-demo",
  "success": false,
  "status": 500,
  "error": "invalid_json",
  "finishedAt": "2026-04-14T10:20:30.123456+00:00"
}
```

### 3.5 字段说明

- `msgId`: 与 `command` 对应的请求 ID
- `bridgeId`: 当前桥实例 ID
- `success`: 是否成功
- `status`: 近似 HTTP 状态码语义
- `data`: 成功时返回的数据；对 `GET /api/seenzus/device-catalog` 或 `GET /api/seenzus/devices`，会返回完整 catalog payload
- `error`: 失败时返回的错误信息
- `finishedAt`: 完成时间

### 3.6 后端要求

- 必须按 `msgId` 关联 `command` 与 `result`
- `result` 只表示“命令执行结果”，不等于设备最终状态
- 如果 `success=true`，仍应继续观察相关 `state`

## 4. state

### 4.1 作用

插件把 HA 实体状态变化主动推送给后端/云端。

### 4.2 方向

插件 -> 后端/云端

### 4.3 Topic

```text
{topicRoot}/bridge/{bridgeId}/state/{entityId}
```

示例：

```text
seenzus/v2/bridge/ha-demo/state/light.living_room
```

### 4.4 Payload

```json
{
  "eventId": "2c188bfd-c947-4d2e-9a70-2b72464b88b2",
  "bridgeId": "ha-demo",
  "entityId": "light.living_room",
  "state": "on",
  "attributes": {
    "brightness": 180
  },
  "ts": "2026-04-14T10:20:30.456789+00:00",
  "source": "ha_state_changed",
  "correlationMsgId": "550e8400-e29b-41d4-a716-446655440000"
}
```

### 4.5 字段说明

- `eventId`: 本次状态事件唯一 ID
- `bridgeId`: 当前桥实例 ID
- `entityId`: 真实 HA 实体 ID
- `state`: 实体主状态
- `attributes`: 实体属性快照
- `ts`: 事件时间
- `source`: 事件来源
- `correlationMsgId`: 可选，若本次状态由某次 `command` 触发，则用于关联该请求

### 4.6 source 语义

当前实现里主要有两种：

- `command`: 由 MQTT `command` 触发的状态回显
- `ha_state_changed`: HA 内部真实状态变化产生的主动推送
- `startup_snapshot`: MQTT 连接成功后启动快照
- `full_snapshot`: `GET /api/states` 命令触发的全量状态快照

### 4.7 后端要求

- `state` 应作为最终设备状态依据
- 后端应按 `entityId` 维护最新状态缓存
- 不要把 `state` 当作可靠历史存储，它本质是事件流
- 如果控制后短时间未收到预期 `state`，可自行做一次兜底查询

### 4.8 特别说明

- 插件会过滤自身诊断实体，避免桥内部状态反复镜像到 MQTT
- 名称(friendly_name)带星号 `*` 的实体(型号/变体标注)不做 state 上报，详见 §5.7
- `state` 默认不 retain
- `startup_snapshot` 和 `full_snapshot` 的 state 发布使用 `qos=0`，其他 state 使用 `qos=1`

## 5. catalog

### 5.1 作用

插件把 HA 设备目录快照推送给后端/云端，用于构建设备列表和统一设备模型。

### 5.2 方向

插件 -> 后端/云端

### 5.3 Topic

```text
{topicRoot}/bridge/{bridgeId}/catalog
```

示例：

```text
seenzus/v2/bridge/ha-demo/catalog
```

### 5.4 触发时机

- MQTT 连接成功后发布一次 `startup_snapshot`
- 收到 `GET /api/seenzus/device-catalog` 或 `GET /api/seenzus/devices` command 后发布一次 `command`

### 5.5 Payload

```json
{
  "eventId": "7e0afc45-77d8-40b2-b488-8b8cc08a18cb",
  "bridgeId": "ha-demo",
  "source": "startup_snapshot",
  "ts": "2026-04-14T10:20:30.456789+00:00",
  "devices": [
    {
      "deviceId": "device-kitchen",
      "name": "Kitchen Lamp",
      "displayName": "Kitchen Lamp",
      "manufacturer": "Acme",
      "model": "L1",
      "areaId": "kitchen",
      "entityCount": 2,
      "availableEntityCount": 2,
      "primaryDomain": "light",
      "online": true,
      "primaryAvailable": true,
      "entities": [
        {
          "entityId": "light.kitchen",
          "name": "Kitchen Light",
          "domain": "light",
          "state": "on",
          "available": true,
          "deviceId": "device-kitchen",
          "areaId": "kitchen"
        }
      ]
    }
  ],
  "deviceCount": 1,
  "entityCount": 2
}
```

### 5.6 字段说明

- `eventId`: 本次 catalog 快照事件唯一 ID
- `bridgeId`: 当前桥实例 ID
- `source`: `startup_snapshot` 或 `command`
- `ts`: 快照生成时间
- `devices`: 设备列表
- `deviceCount`: 设备数量
- `entityCount`(顶层): 所有设备下实体总数
- `correlationMsgId`: 可选，command 触发时用于关联请求

`devices[]` 内每个设备对象字段：

- `deviceId`: HA device registry 的 device id；没有 registry 设备时，使用 `entityId` 构造独立设备
- `entityCount`(设备级): 该设备下实体数量
- `availableEntityCount`: 该设备下 `available=true` 的实体数量;配合设备级 `entityCount` 可体现「部分掉线」程度
- `online`: 当前设备下任一实体 `available=true` 即为 true(语义不变;等价于 `availableEntityCount > 0`)
- `primaryDomain`: 根据实体 domain 优先级推断的主 domain
- `primaryAvailable`: 主域(`primaryDomain`)**任一**实体可用即为 true,反映设备核心功能是否在线;无主域实体时为 `null`。需要更贴近用户体感的在线判断时,优先用此字段而非 `online`

`devices[].entities[]` 内每个实体对象字段：

- `entityId`: HA 实体 id(如 `light.kitchen`)
- `name`: 实体显示名(registry name / original_name / friendly_name / entityId 回退)
- `domain`: 实体 domain(如 `light`/`switch`/`climate`)
- `state`: 当前状态值
- `available`: 状态不为 `unavailable`/`unknown` 即为 true
- `deviceId`: 所属 HA device id;无则为 `null`
- `areaId`: 可选,实体或设备的区域 id
- `deviceClass`: 可选,HA `device_class`(来自 state.attributes,有才发)
- `unit`: 可选,HA `unit_of_measurement`(来自 state.attributes,有才发)
- `icon`: 可选,HA `icon`(来自 state.attributes,有才发)
- `entityCategory`: 可选,HA entity registry 的 `entity_category`,取值 `"config"` / `"diagnostic"`;普通控制实体(`entity_category=None` 或不在 registry)**省略此键**。后端据它把配置/诊断实体踢出控制面

### 5.7 过滤规则

- 插件会过滤自身诊断实体，避免桥内部状态进入设备目录
- 只保留当前 HA 设备 domain 范围内的实体，包括 `light`、`switch`、`climate`、`cover`、`fan`、`lock`、`media_player`、`sensor`、`binary_sensor`、`number`、`select`、`button`、`camera` 等
- 名称(friendly_name / 设备目录里上报的 `name`)中带有星号 `*` 的实体会被过滤(部分集成用 `*` 标注型号/变体)，既不进入 catalog 也不做 state 上报
- 不再因为实体状态是 `unavailable` 或 `unknown` 就从 catalog 中排除；这些实体会保留，且 `available=false`
- `update` 等非设备控制/感知 domain 不进入 catalog

### 5.8 Retain 与 QoS

- `catalog` 使用 `retain=true`
- `startup_snapshot` 使用 `qos=0`
- command 触发的 catalog 使用 `qos=1`

## 6. presence

### 6.1 作用

表示桥实例是否在线，以及当前运行摘要。

### 6.2 方向

插件 -> 后端/云端

### 6.3 Topic

```text
{topicRoot}/bridge/{bridgeId}/presence
```

示例：

```text
seenzus/v2/bridge/ha-demo/presence
```

### 6.4 Payload

```json
{
  "bridgeId": "ha-demo",
  "status": "online",
  "mqttConnected": true,
  "pairingStatus": "bridge_ready",
  "configSource": "web_pair",
  "sourceId": "ha-bridge-0123456789abcdef0123456789abcdef",
  "sourceType": "haos_bridge",
  "sourceName": "MQTT Bridge 01",
  "pairingLastError": null,
  "pairingSessionId": "wps_abc123",
  "ts": "2026-04-22T12:00:00.000000+00:00",
  "requestCount": 12,
  "errorCount": 1,
  "lastError": null,
  "version": "3.0.8"
}
```

### 6.5 字段说明

- `bridgeId`: 当前桥实例 ID
- `status`: `online` 或 `offline`
- `mqttConnected`: 插件当前是否认为 MQTT 已连接
- `pairingStatus`: 快速配对/桥接运行状态
- `configSource`: 配置来源，如 `web_pair` 或手动配置
- `sourceId`: 后端统一数据源 ID
- `sourceType`: 对外数据源类型，当前 HA Bridge 为 `haos_bridge`
- `sourceName`: 数据源展示名
- `pairingLastError`: 最近一次配对错误
- `pairingSessionId`: 快速配对会话 ID
- `ts`: 上报时间
- `requestCount`: 累计请求数
- `errorCount`: 累计错误数
- `lastError`: 最近一次错误
- `version`: 插件版本

### 6.6 Retain 语义

- `presence` 使用 `retain=true`
- 新订阅方会先收到该桥最后一次 retained `presence`

### 6.7 后端要求

- 应把 `presence` 作为桥在线状态的主要判断依据
- 应支持 retained 语义，不要误以为同一桥会长期保留多条在线记录
- 配置变更或 reload 时，常见行为是旧实例 `offline`，随后新实例 `online`
- 如果 `bridgeId` 或 `topicRoot` 变化，旧桥 retained `presence` 会被清理

## 7. 后端消费建议

后端最少应实现这些处理：

- 发布 `command`
- 订阅 `result`
- 订阅 `state`
- 订阅 `catalog`
- 订阅 `presence`

推荐处理顺序：

1. 发布 `command`
2. 等待对应 `result`
3. 继续等待相关 `state`
4. 用 `catalog` 更新设备目录
5. 用 `presence` 判断桥是否在线可用

判断原则：

- `result`: 判断命令是否执行成功
- `state`: 判断设备最终状态
- `catalog`: 判断设备/实体目录
- `presence`: 判断桥是否在线
- `command`: 后端主动发起控制

## 8. 一句话总结

这些事件的职责可以简单理解为：

- `command`：发命令
- `result`：回结果
- `state`：推状态
- `catalog`：同步设备目录
- `presence`：报在线
