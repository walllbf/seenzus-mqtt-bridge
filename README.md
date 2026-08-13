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

- 第一步先选择 `快速配对（推荐）` 或 `手动配置（高级）`
- 第二步进入对应模式的专属表单
- 快速配对页无需填写任何内容（直接提交），随后跳转外部 seenzus 页面完成授权；配对 API 地址走内置生产默认，界面上没有任何输入项。**仅联调本地后端时**，在 HA 配置目录放置 `<config>/seenzus_bridge_dev.json`（内容 `{"pairing_api_base":"http://192.168.x.x:5078/api"}`）即可覆盖；该文件不在插件包内、重装不丢，正常安装无此文件
- 外部授权成功后，浏览器会直接回跳到 HA 本地 callback，由插件自动兑换 MQTT 桥接配置并创建 entry
- 手动配置页仍保留 MQTT 连接参数、手动配对参数和高级参数
- 保存后会自动重载集成，配置立即生效，无需手动重启 HA

| 配置项 | 说明 | 默认值 |
|---|---|---|
| 配对模式 | `seamless` / `manual` | `seamless` |
| 配对 API 地址（快速配对） | 界面无此项；走内置生产地址，联调用 `<config>/seenzus_bridge_dev.json` 覆盖 | 内置生产地址 |
| MQTT Broker 地址 | 手动配置时填写的公网 MQTT 地址 | - |
| MQTT 端口 | Broker 端口 | `1883` |
| MQTT 用户名/密码 | Broker 认证 | 空 |
| V2 Topic 根路径 | v2 协议根路径 | `seenzus/v2` |
| Bridge ID | 留空自动生成稳定 ID | 自动 |
| 启用实体状态事件推送 | 推送 `state` 通道 | `true` |



---

## 🔒 安全部署（重要）

本插件通过公网 MQTT 接收命令并**直接执行 Home Assistant 内部 API（无需 HA Token）**，等于为你的 HA 开放一条远程控制通道。命令通道本身没有应用层鉴权——**安全完全依赖 MQTT Broker 的 Topic ACL 与凭证**。请务必：

- **专用 Broker + 强密码**：不要与不可信方共用 Broker；为本桥单独分配账号。
- **按 bridge 配置 Topic ACL**：`{topicRoot}/bridge/{bridgeId}/command/+` 的 **publish 权限只授予可信后端**；`result / state / presence / catalog` 的 subscribe 权限也应限制到可信方。`bridgeId` 会通过 retained `presence`/`catalog` 广播，不可视为秘密。
- **启用 TLS**：尽量使用 8883/TLS 连接 Broker，避免凭证与设备状态明文传输。

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
  }
}
```

### result 示例

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
提交快速配对表单（API 地址走内置生产默认值；联调时由 <config>/seenzus_bridge_dev.json 覆盖）
  -> 创建带 redirect_uri/state 的 web pairing session
  -> 跳转外部 seenzus 页面
  -> 用户完成授权
  -> seenzus 后端 302 回跳到 HA callback
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

### v0.2.6 (2026-08-14)

- MQTT 完整重连成功后清除历史 `Last error`，诊断状态不再保留已经恢复的断线或发布超时
- 配置重载前的 retained topic 清理改为 best-effort；普通 MQTT 超时只记录汇总告警，仍继续完成集成重载
- 保留 Home Assistant 的任务取消语义，不把 `CancelledError` 当作普通清理失败吞掉
- Bridge ID 变化时同时清理旧 `presence` 与旧 `catalog` retained topic
- Catalog Entity 增加来源范围内稳定的 `registryId` 与主功能命名证据 `originalName`
- 可选发布注册表隐藏/禁用状态，旧消费者与没有注册表证据的 Entity 保持兼容
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
