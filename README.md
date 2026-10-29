# seenzus MQTT Bridge (HAOS Plugin)

seenzus MQTT Bridge 运行在 Home Assistant 本地，通过公网 MQTT 实现云端与局域网 HA 的双向联通，无需内网穿透。

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
seenzus MQTT Bridge (HAOS)
  ├─ 执行 HA 内部 API（无需 HA Token）
  ├─ 回发 result
  └─ 推送 state/presence
```

---

## 安装

兼容性策略：保留最低支持版本 HA Core **2025.1.4**，不设置运行时版本上限。CI 持续验证最低版和官方最新稳定版；测试结果是已验证范围，不是新版本用户能否控制设备的准入开关。手动重跑、隔离测试及版本证据见 [HA 兼容性验证](docs/ha-compatibility.zh-CN.md)。

### HACS（推荐）

1. HA -> HACS -> Integrations
2. 添加自定义仓库（类型 Integration）
3. 安装 `seenzus MQTT Bridge`
4. 重启 Home Assistant

### 手动安装

将 `seenzus_bridge` 目录复制到：

```text
config/custom_components/seenzus_bridge/
```

然后重启 HA。

---

## 配置项

在 HA -> 设置 -> 设备与服务 -> 添加集成 -> `seenzus MQTT Bridge`。

当前配置页行为：

- 添加时选择 `快速配对（推荐）` 或 `手动配置（高级）`，以及设备范围（全部或指定）。
- 快速配对默认跟随应用环境；HA 直接添加可以选择国内 PRD 或境外 PRD，自动识别失败时明确选择一次。
- 从 UAT 应用发起可直接配对，无需开发配置文件。在 HA 中直接选择 UAT／Dev Server 时，在 HA 配置目录放置 `<config>/seenzus_bridge_dev.json`（例如 `{"pairing_api_base":"http://192.168.x.x:5078/api/seenzus"}`）。它只提供开发入口及预设，不修改既有条目；重新配对沿用条目保存的地址。V2 配对要求应用页面与对外配对 API 同源；若服务内部使用不同地址，需配置同源反向代理。自动识别还要求 API 前缀为 `/api/seenzus`；其他前缀需配置代理或从 HA 手动选择开发预设。
- 外部授权成功后，浏览器会直接回跳到 HA 本地 callback，由插件自动兑换 MQTT 桥接配置并创建 entry
- 手动配置页仍保留 MQTT 连接参数、手动配对参数和高级参数
- 保存后会自动重载集成，配置立即生效，无需手动重启 HA

普通用户最多添加国内 PRD、境外 PRD 两个条目；开发入口支持再添加 UAT、Dev Server，总计最多四条。每个环境最多一条，即使选择不同 World 或不同 Dev Server 地址也不能重复添加。一个条目仍绑定一个 World；重新配对从原条目进入，保留原环境、World 和设备范围。HA 管理员在「设置 → 设备与服务 → Seenzus Bridge → 对应条目 → 配置 → 设备授权范围」修改授权。区域选择只展开当前设备，指定范围不会自动包含后来加入的设备；空清单表示不授权任何设备。全部设备模式包含未来新增设备。

受限条目只能读取、控制已授权设备；脚本、场景、模板、任意事件及无法核验目标的操作不会放行。撤权保留历史数据，同时移除当前可用设备。各条目可明确授权相同设备；共享 HA 的底层自动化仍由 HA 管理员负责。实现及发布顺序见 [多环境与设备授权说明](docs/multi-environment-device-access.zh-CN.md)。

| 配置项 | 说明 | 默认值 |
|---|---|---|
| 配对模式 | `seamless` / `manual` | `seamless` |
| 配对环境（快速配对） | 跟随应用／国内 PRD／境外 PRD；开发预设启用 UAT 和自定义地址 | 跟随应用，未识别时选择 |
| MQTT Broker 地址 | 手动配置时填写的公网 MQTT 地址 | - |
| MQTT 传输协议 | 远程 Broker 仅允许 `mqtts` / `wss`；`mqtt` / `ws` 仅限明确的本地 IP | `mqtts` |
| MQTT 端口 | 留空按协议选择；自定义端口请明确填写 | `mqtts:8883` / `wss:443` |
| WebSocket 路径 | 仅 `ws` / `wss` 使用 | `/mqtt` |
| MQTT 用户名/密码 | Broker 认证 | 空 |
| MQTT 最大报文大小（KiB） | 本地发送预算，整数 1–262144；须不高于 Broker 限制 | `1024`（1 MiB） |
| V2 Topic 根路径 | v2 协议根路径 | `seenzus/v2` |
| Bridge ID | 留空自动生成稳定 ID | 自动 |
| 启用实体状态事件推送 | 推送 `state` 通道 | `true` |

已有配对可在集成的「配置 → MQTT 连接设置」修改报文预算，保存后自动重载；
保留凭据、传输方式和 Bridge 身份，无需重新配对。确认 Broker 支持 2 MiB 时
填写 `2048`；这不会修改 Broker 的服务器配置。状态实体的 `mqtt_max_packet_size`
显示当前预算的字节数，配置无效时显示未知并在错误信息中说明原因。

MQTT client ID 使用完整 HA 配置项 ID 的 128 位 SHA-256 摘要，避免相同时间创建
的不同条目因截断 ULID 前缀而互相踢下线。升级后 client ID 会改变，之后重连和
重启保持稳定；MQTT 3.1.1 使用 clean session，不迁移旧会话，配对和 topic 保持原样。
自定义 Broker 若按旧 client ID 配置权限或路由，应同步更新；完全克隆同一个 HA
配置项仍代表同一身份，不能将其作为两个独立实例同时运行。


---

## 🔒 安全部署（重要）

本插件通过公网 MQTT 接收命令并**直接执行 Home Assistant 内部 API（无需 HA Token）**，等于为你的 HA 开放一条远程控制通道。命令通道本身没有应用层鉴权——**安全完全依赖 MQTT Broker 的 Topic ACL 与凭证**。请务必：

- **专用 Broker + 强密码**：不要与不可信方共用 Broker；为本桥单独分配账号。
- **按 bridge 配置 Topic ACL**：`{topicRoot}/bridge/{bridgeId}/command/+` 的 **publish 权限只授予可信后端**；`result / state / presence / catalog` 的 subscribe 权限也应限制到可信方。`bridgeId` 会通过 retained `presence`/`catalog` 广播，不可视为秘密。
- **远程连接强制 TLS**：使用 `mqtts`（通常 8883）或 `wss`（通常 443），使用系统 CA 验证证书及主机名，无跳过校验开关。不会在证书错误或连接失败时降级成明文。

### v0.2.13 连接安全与升级

快速配对返回的 `mqtt.scheme` 必须明确且有效；缺失、空值、未知协议或远程
`mqtt` / `ws` 均拒绝保存。手动配置支持选择 `mqtts` / `wss`、端口和 WebSocket 路径。
这一规则也在每次实际连接前执行，旧配置和 options 覆盖都不能绕过。

已有 `mqtts` / `wss` 配置可继续使用。**旧的远程明文配置或没有协议字段的配置会停止连接**：
在 HA 的「配置 → 重新配对或手动配置」选择安全协议及 Broker 的 TLS 端口，
或者在服务端正确下发安全地址后重新快速配对。不会擅自把旧端口改为 8883，
也不会清除原有凭据、Bridge ID 或 Topic。保存后会自动重载；请确认状态恢复在线。

本地明文仅允许显式 `mqtt` / `ws` 和 IP 字面量：RFC1918、IPv4 回环/链路本地、
IPv6 回环/ULA/链路本地地址。域名（包括 `localhost` / `.local`）一律需要 TLS，
避免 DNS 解析与实际连接之间的地址变化绕过检查。公网、未指定、组播、保留、
文档和 IPv4-mapped IPv6 地址不在明文允许范围内。本地明文仍可能被所在网络监听，
优先使用 TLS；不要把公网转发入口当成本地地址。

这取代早期部署说明中的“先用明文、TLS 后续配置”策略；服务端内部连接方式不由
此插件修改，服务端下发给 HA 的公网地址必须独立配置为安全端点。

### 默认安全开关（v0.1.4+）

执行层默认采用「最小暴露」策略，以下能力默认关闭，仅在确有需要时于「手动配置 → 高级参数」逐项打开：

| 开关 | 默认 | 打开后 |
|---|---|---|
| 允许模板渲染 API | 关 | 放行 `POST /api/template`（任意 Jinja 渲染，信息泄露面大） |
| 允许调用危险服务 | 关 | 放行 `hassio.*` / `shell_command.*` / `python_script.*` / `supervisor.*` 与 `homeassistant.stop` / `restart`；默认这些返回 403，普通设备控制（如 `light.turn_on`）不受影响 |
| 返回完整 config | 关 | `GET /api/config` 返回含家庭经纬度 / 实例 URL 的完整配置；默认裁剪这些敏感字段 |

---

## MQTT Topic 规范（v2）

设：

- `topicRoot = seenzus/v2`
- `bridgeId = ha-xxxx`

则：

- 命令订阅：`{topicRoot}/bridge/{bridgeId}/command/+`
- 结果发布：`{topicRoot}/bridge/{bridgeId}/result/{msgId}`
- 状态发布：`{topicRoot}/bridge/{bridgeId}/state/{entityId}`
- 在线心跳：`{topicRoot}/bridge/{bridgeId}/presence`（retain）

### command 示例

Topic:

```text
seenzus/v2/bridge/ha-demo/command/550e8400-e29b-41d4-a716-446655440000
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
  },
  "operationKey": "opaque-server-operation-key"
}
```

`operationKey` is optional for backward compatibility. When present, the bridge persists an operation fingerprint and result in Home Assistant `.storage`. A completed retry is replayed without calling Home Assistant again. A pre-dispatch claim can be safely recovered after restart; if the bridge restarted after dispatch but before completion was persisted, the operation remains `control_outcome_unknown` and is not automatically dispatched again. Completed and unknown operation keys are retained as durable tombstones so a delayed retry cannot trigger Home Assistant a second time. If a persisted row cannot be parsed safely, its key is frozen as unknown rather than treated as a new operation.

### Bridge 能力声明

新版 Bridge 会在 retained `presence` payload 中声明：

```json
{
  "capabilities": {
    "persistentOperationIdempotency": true
  }
}
```

该字段是向后兼容的新增字段。旧版客户端会忽略它；服务端未收到该声明时，会保留普通控制能力，但不会把 Bridge 当作支持跨重启持久幂等的 Bridge。

### result 示例

MQTT 消息使用紧凑 UTF-8 JSON，字段和值保持不变。发送前按完整 MQTT 3.1.1
PUBLISH 报文（包括 topic、QoS packet ID 和长度头）检查可配置的本地预算，默认 **1 MiB**，
避免默认限额的 Broker 因 `frame_too_large` 主动断开整条桥接连接。
MQTT 3.1.1 不协商这个值；调高 Broker 限额不会自动调高插件的本地预算。

如果命令结果无损编码后仍超限，同一个 `msgId` 会收到 `success: false`、
`status: 413`、`error: "response_too_large"`，并附带 `packetSize` 和
`maxPacketSize`；不会返回截断数据或多条未约定的分片。`GET /api/states`
仍继续通过关联该请求的逐实体 `full_snapshot` 消息同步状态。快照与历史补录中，
单个实体超限会明确记录实体、来源、包大小和上限，并计入错误数及
`presence.droppedStateEventCount`，随后继续发送其他实体，成功计数不包含漏送实体。
这类批次每尝试 50 条短暂让出事件循环，全部超限时也不会连续占住事件循环。
真实传输故障或取消仍中断该批次；历史补录的 30 分钟恢复窗口及完整属性保持不变。
单个实体、服务说明或 retained catalog 自身超过上限时会明确报错；任意大小的数据传输
需要另行扩展协议，不能将“连接保持”理解为该超大响应已完整送达。
启动时 catalog 超限会撤回在线声明并停止本次连接循环，留下明确的大小错误；
修正预算或数据后重载集成再试，不会每 5 秒重复连接。无效预算在建立连接前被拒绝。
初始在线声明也必须发送成功才能进入就绪状态。若离线声明因附带诊断信息而超限，
则发送精简的离线状态以覆盖 retained 在线记录；完整错误仍保留在 HA 诊断中。
若长 Topic 令精简消息仍超限，则退回 App 可解析的 `{"status":"offline"}`。
配置界面会拒绝连这条最小消息也放不下的预算，保留原配置；直接写入此类无效配置
也会在建立连接前被拒绝，需要增大预算或缩短 Topic / Bridge ID。
撤回在线声明时若发生传输故障，会按网络故障重连重试；初始 presence 或目录超限
均在成功撤回后才停止，即使重试附带的错误诊断使初始在线声明变大也不会跳过撤回。

WSS 控制帧回复按连接排队并掩码，在当前 TLS 写入完成后发送；读取保持运行，
使双向大帧都能继续传输。写入重试保留原字节，控制队列有界并在连接关闭时清理。
控制回复的终止性 socket/TLS 写入故障会进入 MQTT 重连流程，兼容新旧 aiomqtt；
程序错误仍按错误上报，取消操作仍能停止连接。
最终候选版本的实机配对验收见 [配对验收清单](docs/PAIRING_ACCEPTANCE.zh-CN.md)。

Topic:

```text
seenzus/v2/bridge/ha-demo/result/550e8400-e29b-41d4-a716-446655440000
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
seenzus/v2/bridge/ha-demo/state/light.living_room
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
- 插件会过滤自己创建的诊断实体（如 `sensor.seenzus_mqtt_bridge_*`），不会再把这些内部状态镜像到 MQTT
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
选择条目的环境和设备范围（应用来源可自动衔接环境）
  -> 创建带 redirect_uri/state 的 web pairing session
  -> 跳转外部 seenzus 页面
  -> 用户完成授权
  -> seenzus 后端 302 回跳到 HA callback
  -> 插件完成 state 校验与 code exchange
  -> 自动拿到 mqtt.host / mqtt.port / mqtt.username / mqtt.password / topicRoot / bridgeId
  -> 按环境唯一创建 entry；环境已有条目时引导管理原条目，重新配对保留原设备范围
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

插件会创建传感器：`seenzus MQTT Bridge 状态`，包含：

- `request_count`, `result_count`, `state_push_count`, `error_count`
- `topic_root`, `bridge_id`
- `pairing_mode`, `pairing_status`, `pairing_session_id`
- `pairing_expires_at`, `verification_code`, `pairing_bound_at`
- `config_source`
- `pairing_last_step`, `pairing_last_api_base`
- `last_error`

配对接口调用可观察性：

- 快速配对会记录创建 web session、外部授权完成、MQTT 配置落地、bridge 启动等步骤日志
- 也可以直接在 `seenzus MQTT Bridge 配对状态` 实体属性里查看 `pairing_last_step` 和 `pairing_last_api_base`

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

- Home Assistant 2025.1.4+
- Python 3.11+（HA 内置）
- 公网 MQTT Broker（推荐 EMQX Cloud / HiveMQ）

---

## 测试与验证

仓库已补充隔离测试环境与行为测试，当前覆盖重点包括：

- 内部诊断实体不会被重复镜像到 `state`
- 普通实体状态变化会被发布到正确的 `state` topic
- 配置变更触发 reload 前会清理旧桥 retained `presence`
- reload 流程会在清理后继续调用配置项重载
- 在线状态、新建快速配对和重新配对上报的版本号与 `manifest.json` 一致

升级集成版本时，同时更新 `manifest.json` 和 `const.py` 中的 `BRIDGE_VERSION`；行为测试会检查对外发送的版本号，防止两者再次不一致。

按 [兼容性说明](docs/ha-compatibility.zh-CN.md) 解析版本并创建隔离环境。例如使用 Python 3.13 验证最低版：

```text
python -m venv .venv-test
.\.venv-test\Scripts\python -m pip install -r requirements_contract.txt homeassistant==2025.1.4 pytest==8.3.4 pytest-asyncio==0.24.0 pytest-timeout==2.3.1
```

运行测试：

```powershell
$env:EXPECTED_HA_VERSION='2025.1.4'
.\.venv-test\Scripts\python -m pytest tests -q
```

测试覆盖对照表见：

```text
docs/test-coverage-matrix.md
```

快速配对完整流程（含后端契约）见：

```text
docs/quick-pair-flow.zh-CN.md
```

MQTT 事件契约见：

```text
docs/MQTT_BRIDGE_EVENTS_SPEC.zh-CN.md
```

---

## 版本变更记录

### v0.2.16 (2026-10-29)

- 多环境：国内 PRD、境外 PRD 可在同一 HA 中各建一个独立条目（有效开发预设另开放 UAT / Dev Server），每个条目独立保存 World、连接和设备授权；同一环境重复添加会在兑换凭证前拦截
- 设备授权：按区域、设备、独立实体逐步授权；目录、实时状态、读取、控制、快照和历史补录统一按授权执行，撤权不删除历史
- 两阶段配对提交：服务端先签发候选凭证，HA 把条目保存到磁盘后才提交。授权后取消、重配时断网或重启 HA、服务端拒绝后，HA 与应用回到同一连接
- 从 Android、iOS、鸿蒙应用发起配对时，HA 自动进入发起环境，不再依赖 `window.opener`，也无需选择环境或输入引导码；无法识别时在识别页直接选择环境
- 手机装有 Home Assistant App 时，授权回到浏览器后 HA 自行完成保存和提交
- 配对成功页和 HA 通知提供「返回 seenzus 应用」，直接打开刚配对的数据源
- 识别页因「该环境已有条目」或「需手动选择环境」退回时，回调页说明原因，不再提示正在保存
- 兼容提示：条目版本升为 2，旧条目自动迁移并保留原地址、凭证和全部设备授权。回退到 0.2.15 及更早版本需恢复升级前的 HA 备份。手机端自动识别需要应用服务端包含 seenzus PR #2241 与 #2247（国内、境外 PRD 已部署）；未升级的服务端退回立即提交，不影响配对本身

### v0.2.15 (2026-10-21)

- HA 正常关机在后台 MQTT 任务取消前发送离线状态；清理保留超时上限，手动卸载后注销关机任务
- 传感器移除时注销更新回调，避免禁用后继续写入和重新添加后重复更新
- 配对模式、MQTT 协议、实体名称与状态、配对通知支持英语和简体中文；其他语言回退英语
- MQTT 凭据保留首尾空格；区分未提供密码与空密码
- 兼容提示：状态传感器的底层 state 改为稳定代码 `starting` / `active` / `error` / `stopped`，界面按语言显示。直接匹配旧中文 state 的自动化需改用对应代码；实体 unique ID 保持不变

### v0.2.14 (2026-10-21)

- 补齐 `translations/en.json`，修复英文及没有专用翻译的语言在配置界面缺少标题、字段名称和错误提示的问题
- `strings.json` 使用英文源文案并与英文运行时翻译同步；保留简体中文翻译
- 使用实际 Home Assistant 翻译加载器验证首次配置、选项与启动错误的英文、法语回退及简体中文显示

### v0.2.13 (2026-10-19)

- 修复 HACS 收录审核指出的远程 MQTT 明文传输问题：配置、配对与实际连接统一强制 TLS，缺失或未知协议不再回退 TCP
- 手动配置支持 MQTT over TLS / 安全 WebSocket、端口及路径，保留系统证书和主机名校验
- 旧的不安全配置停止连接并提示重新配置；本地明文仅限显式选择的内网或回环 IP，见上方升级说明
- 被安全检查拦截的配置，在保存安全协议或重新配对后自动重载，无需手动重启 HA

### v0.2.12 (2026-10-16)

- 集成依赖改为受限的兼容版本范围，由 HA 的运行时约束选择 MQTT 库：HA 2025.1.4 使用 aiomqtt 2.0.1 / Paho 1.6.1，新版 HA 使用 aiomqtt 2.5.x / Paho 2.1.0，修复 0.2.11 因依赖冲突加载失败、无法配对的问题；保留 WSS 写入保护
- 启动时同时检查已安装库与 Core 约束、aiomqtt 的 Paho 依赖，不兼容时通过 HA 安装器修复；若旧库已被其他集成导入，日志会提示重启 HA 后加载已修复的版本
- CI 使用所选 Core 的运行时约束安装 manifest 依赖，并在独立环境预装不兼容的 MQTT 库，通过真实 HA 安装器验证升级修复路径
- 集成版本、配对上报和 presence 统一为 0.2.12，以便区分旧的 0.2.11 下载包与包含依赖修复的新包；升级后重启 HA，已有有效配对会保留
- MQTT 本地报文预算可配置，默认 1024 KiB；已确认 Broker 上限为 2 MiB 时可在「配置 → MQTT 连接设置」填写 2048 KiB。预算以完整 MQTT PUBLISH 字节数检查，超限明确报错
- MQTT client ID 根据完整 entry ID 生成稳定摘要，避免不同实例因 ULID 前缀相同互相顶下线；WSS 控制回复与 TLS 写入协调，旧版 MQTT 库的断写也能进入重连流程
- 支持协商的完整快照流和服务名称索引，减少聚合 Result 和完整服务描述的重复传输；保留完整实体属性、完成证明和旧接口兼容
- 包含 HA 2026.10 实体注册表适配；用户现场反馈加载、同步及重启后持续控制正常。独立真实 HA / App / EMQX 的全新配对、WSS、断网补录、大报文、多实例和 10 分钟稳定性已完成，详见 [跨端验收记录](docs/verification/0.2.12/README.zh-CN.md)

如果升级后仍显示「未加载」，先检查日志是否仍要求 `aiomqtt==2.4.0`：这表示 HA 当次读取的仍是旧依赖声明。完整覆盖集成目录并重启，避免仅替换个别 Python 文件。
`[code:135] Not authorized` 则表示 Broker 拒绝认证；检查当前配对对应的 Broker 用户及凭据是否有效。安装依赖修复不会恢复已吊销的凭据，需要通过现有集成的配对设置重新授权，不必先删除配置条目。

### v0.2.11 (2026-09-30)

- 状态事件携带完整 HA 属性；瞬态 event/button 逐次上报，事件时间使用 HA 原始观测时间，稳定 eventId 使重试和补录复用同一事实身份（#61）
- 启动和重连后补录最近 30 分钟的 recorder 历史，设置每实体和全局上限，溢出计入丢弃计数；未启用 recorder 时保留实时转发（#61）
- 修复 WSS 业务 TLS 写入因背压待重试时，PING/CLOSE 回复插入写入引发的 BAD_LENGTH；写入完成后恢复接收，保留并发 QoS 确认（#63）
- 发送失败后停止当前命令的后续发送，重连等待前取消旧命令及连接后台任务，减少断线后的连锁错误（#63）
- 运行与测试固定 aiomqtt 2.4.0 / Paho 2.1.0，纳入真实 TLS/WSS 背压与协调器恢复回归
- manifest、在线状态和配对上报版本统一为 0.2.11；已有配置和配对信息保留，升级后重启 Home Assistant

### v0.2.10 (2026-09-21)

- HA 设备名称、型号、区域等 Registry 信息变化后，约 1.5 秒合批推送最新 retained catalog；此目录监听独立于实时状态推送开关，并清理卸载时的监听和任务（#57）
- 实体名称、精度、翻译和 HA 时区等显示元数据随状态事件、快照与状态回读传递；Registry 名称变化触发刷新，保留 HA 原始状态（#51 / #53）
- Catalog 增加原始 `modelId`，为应用识别已知电表的电量清零用途提供型号依据；增加 HA config entry 分组信息，为应用设备归属提供依据，不传递账号凭据（#48 / #53）
- 在线状态与配对信息上报的版本号和 manifest 保持一致，修复旧版本号残留（#55）
- 兼容性 CI 持续验证最低支持版与官方最新稳定版 HA，并增加真实 HA 目录、状态和服务契约测试（#50）
- 已有配对与配置继续保留，升级后重启 Home Assistant；名称和清零显示效果需要配套新版 Seenzus 应用。本版本不包含开发中的多条目、多环境配对功能

### v0.2.9 (2026-09-01)

- 全量状态快照分批发布，断线状态积压按实体合并并限制容量，缓解 MQTT 发布突发与恢复积压（#47）
- 保留并发 ACK 等待能力，避免心跳、命令结果和状态发布的队头阻塞；已有配置与配对信息保持兼容

### v0.2.8 (2026-08-30)

- MQTT 启动顺序调整为先发布保留设备目录并进入 ready / command pump，再在后台执行最佳努力的全量状态快照；大型 HA 实例的初始快照被中断时，重连不再从零重放并阻塞设备上线（#45 / #46）
- Quick Pair 回调读到 Seenzus App 会话标记（`?app=1`，由服务端在授权会话来自 App 内嵌 WebView 时附加）后，由插件服务端自己把 config flow 从 `EXTERNAL_STEP_DONE` 推进到终态（`create_entry` / `abort` / 表单），App 内壳全程配对不再依赖常驻 HA 前端标签页，集成条目可靠建立
- 推进步数带上限（`MAX_FLOW_RESUME_STEPS=4`）防死循环，耗尽上限仍未到终态时明确报错；`UnknownFlow` 视为成功，兼容前端标签页抢先完成配对的竞态
- 浏览器路径（无 `app` 标记）保持单次 configure，网页版行为不变；回调页 `window.close()` 无效时 300ms 兜底跳转集成页，不再白屏
- 测试 fake 的 `async_configure` 升级为可脚本化结果队列，能跑出真实 HA 的两步推进语义（本 bug 即由旧 fake 恒返 None 漏过）
- 配套文档补充插件回调 view 两条路径的行为说明（`docs/quick-pair-flow.zh-CN.md` §5.1）
- 生产真机验收：App 内壳全程与桌面浏览器两条路径均验证通过（#43）
- 全量测试 **225 passed**，HA Core 双版本、Hassfest 与 HACS 校验经 CI 通过

### v0.2.7 (2026-08-16)

- MQTT 控制命令支持可选 `operationKey`，使用 Home Assistant `.storage` 持久化操作领取、下发与完成状态
- 已完成操作可安全重放原结果；指纹冲突、损坏记录及下发后结果未知均 fail closed，持久 tombstone 防止延迟重试再次触发设备
- retained `presence` 声明 `capabilities.persistentOperationIdempotency=true`，让服务端仅在 Bridge 明确支持时启用跨重启幂等控制
- 修复 MQTT 任务阻塞 Home Assistant 配置项加载、重载和卸载生命周期的问题
- 品牌 Logo 全套更新：同步替换 HACS 与组件内 256 / 512 像素透明底图标及源 SVG
- 不携带 `operationKey` 的旧版控制请求保持原有行为，旧版客户端会忽略新增 presence 能力字段
- 全量测试 **214 passed**，HA Core 双版本、Hassfest 与 HACS 校验通过

### v0.2.6 (2026-08-14)

- MQTT 完整重连成功后清除历史 `Last error`，诊断状态不再保留已经恢复的断线或发布超时
- 配置重载前的 retained topic 清理改为 best-effort；普通 MQTT 超时只记录汇总告警，仍继续完成集成重载
- 保留 Home Assistant 的任务取消语义，不把 `CancelledError` 当作普通清理失败吞掉
- Bridge ID 变化时同时清理旧 `presence` 与旧 `catalog` retained topic
- Catalog Entity 增加来源范围内稳定的 `stableEntityId`，并以 `isMainFeature` 标记注册表主功能命名证据
- 可选发布 `hidden` / `disabled` 注册表状态，旧消费者与没有注册表证据的 Entity 保持兼容
- 全量测试 **194 passed**，HA Core 双版本、Hassfest 与 HACS 校验通过

### v0.2.5 (2026-08-08)

- Catalog 上报真实 Home Assistant Core 版本和运行时 Action Catalog，为 seenzus 的版本化能力门与安全反控提供证据
- 保留所有绑定 HA Device 的 Entity 及其启动快照、全量快照、状态事件和命令回读；独立 Entity 仍只允许官方 Platform 或明确 Helper
- `unknown` 表示值未知但 Entity 仍可用，只有 `unavailable` 表示不可达
- Helper 白名单补齐 `input_text`、`input_datetime`
- CI 固定验证 HA Core 2025.1.4 / Python 3.13 与 HA Core 2026.8.0 / Python 3.14.2
- 两个真实 Core 版本下完整测试均为 **190 passed**，Hassfest 与 HACS 校验通过

### v0.2.4 (2026-07-31)

- MQTT command 处理增加 8 个在飞任务的硬上限，超额消息触发负载保护，不再为每条消息无限创建 task
- 插件停止时统一取消并回收仍在运行的 command task，避免重载/卸载后残留
- 测试 **183 passed**

### v0.2.3 (2026-07-20)

- Catalog 新增 `wireVersion=2.1` 与来源级 `isComplete=true`，让服务端只用完整快照判 soft-missing
- 星号过滤收窄为末尾含数字的 ASCII 型号 token；`客厅灯*` 等用户命名不再被静默过滤
- 测试 **182 passed**

### v0.2.2 (2026-07-14)

- **品牌图标更换为新版云朵图标**：`brand/` 全套（icon/logo 及 @2x）与组件内图标同步替换，源 SVG 经 Chromium 渲染为透明底 PNG，渐变与滤镜特效无损
- 品牌小写补漏：`hacs.json` 名称改为 `seenzus MQTT Bridge`（HACS 列表显示随之修正）
- 文档安全清理：移除文档中的真实 MQTT 凭据与内部测试地址（统一为占位符），安装指南截图对家庭内网设备信息打码
- 已提交 HACS 官方默认库收录申请（hacs/default#9163，自动校验全部通过，排队人工审核中）
- 本版无代码逻辑变更，测试基线同 v0.2.1（179 passed）

### v0.2.1 (2026-07-05)

- **配对 API 地址改用本地覆盖文件**：快速配对表单彻底无输入项，配对地址走内置生产默认；仅联调本地后端时，在 HA 配置目录放置 `<config>/seenzus_bridge_dev.json`（`{"pairing_api_base":"http://…"}`）覆盖。该文件不在插件包内、重装不丢，故联调包与上线包逐字节同一份（详见 `custom_components/seenzus_bridge/dev_override.py`）
- **选中「快速配对」即直接发起配对**：跳过原来那屏"直接提交"确认页，成功即跳外部授权、失败才回落到带错误提示的表单
- 兼容说明：HA 2026.6 已移除用户资料的「高级模式」开关，本版不再依赖它做任何门控
- 测试 **179 passed**

### v0.2.0 (2026-07-03)

- **支持 MQTT over WebSocket（wss）/ mqtts 传输**（issue #14，配合后端 CDN 前置抗封锁）：快速配对兑换响应下发 `scheme`/`path` 时，桥经 `wss://edge.seenzus.ai:443/mqtt`（Cloudflare 前置）连接；旧 entry 与裸 TCP 下发行为完全不变，无新依赖
- 传输健壮性：scheme/path 显式落盘（重配对不残留旧传输配置）、显式 JSON null 归一、缺 port 按 scheme 取惯例端口（wss 443 / mqtts 8883）、TLS 上下文缓存；keepalive 保持 60s 适配 CF 空闲窗口
- **通知返回链接点击即关**：「返回 seenzus 应用」链接改走签名中转端点，点击后自动关闭通知再跳转（深链走 HTML 跳转页）；修复链接被前端拦截成 SPA 路由导致点击无效的问题
- 诊断可观测性：状态传感器新增 `mqtt_transport`/`mqtt_ws_path` 属性，presence 载荷新增 `transport`/`wsPath`；快速配对失败诊断收敛到「配对状态」传感器属性（原先只在日志与通知）
- 测试 **170 passed**

### v0.1.9 (2026-07-01)

- 品牌显示名统一小写为 `seenzus MQTT Bridge`（`MQTT` 保持大写；集成名、传感器、通知、配置页文案全覆盖）
- 桥名附带 HA 家名（`seenzus MQTT Bridge · 我的家`）以在 seenzus App 桥列表区分多个家；家名做净化（去控制字符、截断超长）
- 配对会话新增 `haInstanceId`（HA 稳定实例 UUID），供后端识别同一 HA 重配、避免重复僵尸桥（前向兼容，见 `docs/HANDOFF_REPAIR_DEDUP.zh-CN.md`）
- 返回链接（成功页 + 持久化通知）改为 H2 大字并增加间距，更醒目
- 更新集成图标 / logo（本地 `brand/`，走 HA 2026.3 本地品牌图机制）
- 品牌名收敛为单一常量 `const.PRODUCT_NAME`

### v0.1.6 (2026-06-25)

- 更新集成图标为正式 seenzus 图标（v0.1.5 打包的是占位图；升级到本版后 HA 集成界面将显示新图标）
- 仓库更名为 `seenzus-mqtt-bridge`，更新 `documentation` / `issue_tracker` 链接
- 文档统一归入 `docs/`，新增图文安装指南 `docs/haos-seenzus-mqtt-bridge-guide.zh-CN.md`

### v0.1.5 (2026-06-25)

- 统一显示品牌为 **seenzus**（原 seenzusAI）：集成显示名改为 `seenzus MQTT Bridge`
- 诊断实体 entity_id 前缀随之变为 `seenzus_mqtt_bridge_`；`entity_filters` 同步更新并保留旧前缀（`seenzusai_mqtt_bridge_` / `savanai_bridge_`）兼容旧安装

### v0.1.4 (2026-06-24)

- 命令执行层引入默认安全策略：模板渲染、危险服务调用、完整 `GET /api/config` 默认关闭，可在「手动配置 → 高级参数」逐项放开
- `GET /api/config` 默认裁剪家庭经纬度与实例 URL 等敏感字段
- 快速配对默认地址切换到生产环境 `https://seenzus.ai/api/seenzus`
- 统一品牌为 seenzus，修复诊断实体过滤前缀（`seenzusai_mqtt_bridge_`，兼容旧前缀）
- 默认 Topic 根路径统一为 `seenzus/v2`
- 新增 README「安全部署」章节

### v0.1.3 (2026-06-24) — 首个 HACS 公开发布

- HACS 合规结构、MIT 许可证、hassfest + HACS Action CI 校验
- 快速配对：外部页授权成功后直接回跳 HA callback + code exchange 自动回写 MQTT 配置
- 配置页两段式模式选择（快速配对 / 手动），保存后自动重载集成
- 后端 `web-pairing/session` 新增 `redirectUri/state` 契约，新增 callback code exchange 接口
- 新增回跳状态校验、授权失败/超时/兑换失败错误提示与 callback 测试覆盖

### v0.1.2 — 早期迭代

- 配置页改为单页折叠分组，保存后自动重载
- 过滤桥自身诊断实体，避免 `state` 自激循环
- 当 `bridgeId` / `topicRoot` 变化时清理旧桥 retained `presence`
- 修复状态事件监听导致的 MQTT 发布回路

### v0.1.1 — 初始版本

- 按 `bridgeId` 隔离 Topic，命令 / 结果 / 状态三通道
- 运行在 HA 本地，通过公网 MQTT 实现云端与局域网双向联通
- 支持快速配对与手动 MQTT 桥接两种配置方式
