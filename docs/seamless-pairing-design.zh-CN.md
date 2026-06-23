# SavanAI Bridge 无感配对设计

本文档描述 `SavanAI Bridge` 的无感配对方案，目标是在保留现有手动配置模式的前提下，新增一条以云应用发起为主、HA 插件端低输入成本的“快速配对”流程。

相关文档：

- `docs/seamless-pairing-implementation-plan.zh-CN.md`
- `../seenzusaibackend/docs/ha-pairing-bootstrap-api.zh-CN.md`

---

## 1. 设计目标

目标用户体验：

- 用户主要在云应用里点击“添加 Home Assistant”
- 手机端打开 HA 配对页时，支持直接扫描二维码内容
- PC 端打开 HA 配对页时，支持直接粘贴配对链接
- 用户不需要手填 `Seenzus API 地址`
- 用户不需要理解 `Pairing Token / Pairing Code`
- 插件在拿到链接后自动完成 bootstrap、claim 和绑定状态轮询

约束条件：

- 保留现有手动配置模式作为高级兜底
- 不破坏当前 MQTT 主链路
- 插件与后端通过短时、一次性、可撤销的配对上下文交互
- 配对链接不是最终长期凭证

---

## 2. 总体方案

本方案仅保留两种“输入配对信息”的方式：

1. 手机端扫码二维码
2. PC 端粘贴同一个配对链接

不再支持短码。

两种输入方式在插件内部统一为同一条逻辑链路：

```text
二维码 / 配对链接
    -> 解析配对链接
    -> 调后端 bootstrap
    -> 获取一次性配对上下文
    -> 自动执行 claim
    -> 轮询/等待云端确认
    -> 绑定完成
```

---

## 3. 为什么配对链接不能直接作为最终凭证

配对链接本质上更适合作为“进入配对会话”的入口，而不是长期凭证，原因如下：

- 链接更容易泄露：二维码截图、剪贴板、浏览器历史、日志都可能暴露链接
- 长期凭证需要支持撤销、轮换、迁移，链接不适合承担这类职责
- 后端需要保留决策权，决定该会话是否允许被 claim、是否过期、是否需要确认
- 配对链接更适合是短时、一次性、可校验的授权票据

因此建议分层：

- `配对链接`：进入会话
- `一次性 pairing_token`：完成本次 claim
- `最终桥身份 / 绑定关系`：长期运行

---

## 4. 插件侧配置流设计

### 4.1 入口页

插件配置入口分为两种模式：

- `快速配对（推荐）`
- `手动配置（高级）`

默认进入 `快速配对`。

### 4.2 快速配对页

页面核心字段：

- `Seenzus API 地址`

页面交互：

- 插件先调用 `POST /integrations/ha/pairing/session`
- 获取 `pairingLink` 后跳转到外部 Seenzus 页面
- 用户在外部页面完成授权后，插件返回并继续 bootstrap
- bootstrap 成功后，插件直接把 MQTT 桥接配置写入 config entry

说明文案：

- 只需填写 Seenzus API 地址
- MQTT 连接和桥接参数由后端自动下发，无需手填

### 4.3 配对处理中页

外部授权完成后，配置流进入处理中状态，展示当前步骤：

- 正在创建 quick pair session
- 正在跳转外部 Seenzus 页面
- 正在 bootstrap 并获取 MQTT 配置
- 正在写入 Home Assistant 集成配置
- 等待 MQTT 建桥与云端确认

### 4.4 配对完成页

显示：

- 当前 `bridgeId`
- 当前绑定状态
- 配对时间
- 操作按钮：
  - `重新配对`
  - `改用手动配置`

### 4.5 手动配置页

现有字段保留，但归类为高级模式：

- MQTT 参数（仅手动配置模式）
- Topic / Bridge 高级参数
- 旧配对字段

---

## 5. 二维码和配对链接格式设计

### 5.1 推荐格式

推荐直接使用标准 HTTPS 链接：

```text
https://app.seenzus.xxx/ha/pair?session=ps_abc123&nonce=n_xyz789
```

也可选支持自定义 scheme：

```text
seenzus://ha/pair?session=ps_abc123&nonce=n_xyz789
```

推荐优先 HTTPS，原因：

- 浏览器兼容性最好
- PC 粘贴最自然
- 手机扫码行为更稳定

### 5.2 链接中允许包含的字段

建议仅放：

- `session`
- `nonce`
- 可选 `env`
- 可选签名字段（如果后端需要）

例如：

```text
https://app.seenzus.xxx/ha/pair?session=ps_abc123&nonce=n_xyz789&sig=abc
```

### 5.3 链接中不应包含的字段

不要直接放：

- 最终长期凭证
- 长期桥身份 token
- MQTT 账号密码
- 长期 `shared_secret`
- 用户敏感身份信息

### 5.4 插件解析要求

插件收到链接后应校验：

- scheme / 域名是否合法
- 是否包含 `session`
- 是否包含 `nonce`
- 是否过长或格式异常
- 是否来自允许的环境域名

解析输出最小载荷：

- `session_id`
- `nonce`
- `source_url`

---

## 6. 插件侧状态机

建议插件内部统一状态：

- `idle`
- `waiting_external_auth`
- `mqtt_config_received`
- `bridge_starting`
- `bridge_ready`
- `bootstrapping`
- `bootstrap_failed`
- `claiming`
- `claimed_pending_confirm`
- `bound`
- `expired`
- `claim_error`

对应用户可见文案建议：

- 等待外部授权完成
- 已收到自动桥接配置
- MQTT bridge 正在启动
- MQTT bridge 已连通
- 正在获取配对凭证
- 正在声明此 Home Assistant
- 等待云端确认
- 配对成功
- 配对已过期，请重新扫码
- 配对失败，请重试

---

## 7. 插件侧数据结构

### 7.1 PairingLinkPayload

插件解析链接后的载荷：

- `session_id`
- `nonce`
- `source_url`
- `expires_at`（可选）

### 7.2 PairingBootstrapResult

插件向后端 bootstrap 后拿到：

- `session_id`
- `api_base`
- `pairing_token`
- `shared_secret`（可选）
- `mqtt.host`
- `mqtt.port`
- `mqtt.username`
- `mqtt.password`
- `mqtt.topicRoot`
- `expires_at`
- `status`
- `message`

### 7.3 PairingRuntimeState

插件运行态保存：

- `pairing_mode`
- `pairing_status`
- `pairing_session_id`
- `pairing_expires_at`
- `pairing_verification_code`
- `pairing_last_error`
- `pairing_bound_at`

不建议持久保存原始配对链接。

---

## 8. 插件需要新增的模块

建议拆分以下文件，避免把逻辑继续堆进 `__init__.py`：

- `pairing_link.py`
  - 负责链接校验与解析
- `pairing_bootstrap.py`
  - 负责请求 bootstrap / status 接口
- `pairing_state.py`
  - 负责配对状态与状态转换
- `config_flow.py`
  - 接入快速配对模式

---

## 9. 后端服务职责

后端建议拆为 3 层：

- 配对会话层
- bootstrap 换参层
- claim / 绑定确认层

### 9.1 配对会话模型

建议字段：

- `session_id`
- `user_id`
- `status`
- `nonce`
- `pairing_link`
- `expires_at`
- `bridge_id`
- `bridge_name`
- `bridge_version`
- `claimed_at`
- `confirmed_at`
- `rejected_at`
- `verification_code`
- `pairing_token`
- `metadata`

建议状态：

- `pending`
- `bootstrapped`
- `claimed`
- `confirmed`
- `expired`
- `rejected`
- `cancelled`

---

## 10. 后端接口设计

### 10.1 创建配对会话

```text
POST /integrations/ha/pairing/session
```

请求：

```json
{}
```

响应：

```json
{
  "ok": true,
  "sessionId": "ps_abc123",
  "pairingLink": "https://app.seenzus.xxx/ha/pair?session=ps_abc123&nonce=n_456",
  "expiresAt": "2026-04-20T12:00:00Z",
  "status": "pending"
}
```

### 10.2 bootstrap

```text
POST /integrations/ha/pairing/bootstrap
```

请求：

```json
{
  "sessionId": "ps_abc123",
  "nonce": "n_456",
  "bridgeName": "SavanAI Bridge",
  "bridgeVersion": "3.0.8",
  "platform": "homeassistant",
  "haVersion": "2026.3.0"
}
```

响应：

```json
{
  "ok": true,
  "sessionId": "ps_abc123",
  "status": "bootstrapped",
  "apiBase": "https://api.seenzus.xxx",
  "pairingToken": "pt_xxx",
  "sharedSecret": "ss_xxx",
  "expiresAt": "2026-04-20T12:05:00Z"
}
```

### 10.3 claim

```text
POST /integrations/ha/pairing/claim
```

请求：

```json
{
  "pairingToken": "pt_xxx",
  "bridgeId": "ha-01kpdc68hfhc",
  "bridgeName": "SavanAI Bridge",
  "bridgeVersion": "3.0.8"
}
```

响应：

```json
{
  "ok": true,
  "status": "claimed",
  "sessionId": "ps_abc123",
  "verificationCode": "123456"
}
```

### 10.4 查询配对状态

```text
GET /integrations/ha/pairing/session/{sessionId}/status?bridgeId=ha-01kpdc68hfhc
```

响应：

```json
{
  "ok": true,
  "sessionId": "ps_abc123",
  "status": "claimed",
  "bound": false,
  "expiresAt": "2026-04-20T12:05:00Z"
}
```

确认成功后：

```json
{
  "ok": true,
  "sessionId": "ps_abc123",
  "status": "confirmed",
  "bound": true,
  "confirmedAt": "2026-04-20T12:01:22Z"
}
```

### 10.5 云端确认绑定

```text
POST /integrations/ha/pairing/session/{sessionId}/confirm
```

请求：

```json
{
  "bridgeId": "ha-01kpdc68hfhc"
}
```

响应：

```json
{
  "ok": true,
  "status": "confirmed",
  "bridgeId": "ha-01kpdc68hfhc"
}
```

---

## 11. 插件与后端的时序

### 11.1 用户视角

1. 云应用点击“添加 HA”
2. 云应用展示二维码 / 配对链接
3. 用户在 HA 插件里扫码或粘贴链接
4. 插件自动连接 Seenzus
5. 云应用显示“发现一个 HA 插件”
6. 用户在云端确认
7. 绑定成功

### 11.2 系统视角

```text
云应用 -> 后端: 创建 PairingSession
后端 -> 云应用: 返回 pairingLink
插件 -> 后端: bootstrap(sessionId, nonce)
后端 -> 插件: 返回 apiBase + pairingToken
插件 -> 后端: claim(pairingToken, bridgeId)
后端: 记录 bridgeId 与 session 绑定
插件 -> 后端: 轮询 session status
云应用 -> 后端: confirm(sessionId, bridgeId)
后端 -> 插件: status = confirmed
插件: 写入 bound 状态
```

---

## 12. 安全边界

后端必须保证：

- 配对链接短时有效
- `nonce` 必须校验
- bootstrap 调用有限次且可过期
- `pairing_token` 一次性使用
- claim 必须和 `session_id` 严格关联
- 确认绑定必须由当前云端登录用户完成
- 会话支持 `expired / rejected / cancelled`

插件必须保证：

- 不长期保存原始配对链接
- 不把链接直接当长期凭证
- 不在日志中输出明文 token
- 配对失败时清理临时上下文

---

## 13. 异常流设计

### 13.1 链接非法

表现：

- 无法解析 `session` 或 `nonce`

插件处理：

- 直接提示“配对链接无效”
- 不进入 bootstrap

### 13.2 会话过期

表现：

- bootstrap 返回 `expired`
- 或 status 轮询返回 `expired`

插件处理：

- 设置 `pairing_status = expired`
- 引导用户回云应用重新生成二维码

### 13.3 claim 失败

表现：

- `/claim` 返回失败

插件处理：

- `pairing_status = claim_error`
- 记录 `last_error`
- 保留重试或重新扫码入口

### 13.4 云端拒绝

表现：

- status 返回 `rejected`

插件处理：

- 显示“配对已被拒绝”
- 清理临时上下文

---

## 14. 与现有手动模式的兼容

建议双模式并存：

- `快速配对（推荐）`
- `手动配置（高级）`

现有字段保留兼容：

- `pairing_api_base`
- `pairing_token`
- `pairing_code`
- `pairing_shared_secret`

但新用户默认不再接触这些字段。

---

## 15. 建议实现顺序

### 第一阶段

- 后端实现 `session create`
- 后端实现 `bootstrap`
- 插件实现链接解析
- 插件实现 bootstrap 调用

### 第二阶段

- 插件实现自动 claim
- 后端实现 `session status`
- 云应用实现“发现到的桥”展示

### 第三阶段

- 云应用实现确认绑定
- 插件实现 `bound` 状态落地
- 手动模式降级为高级入口

---

## 16. 插件侧最终结论

插件侧无感配对的最终形态应为：

- 手机端：扫码二维码，自动填入配对链接
- PC 端：粘贴同一个配对链接
- 插件不再要求用户手填后端地址和 token/code
- 插件只把链接当作进入配对会话的入口
- 真正用于 claim 的凭证由后端 bootstrap 接口短时签发
- claim 成功后，插件等待云端确认，随后完成绑定

这套方案兼顾：

- 无感体验
- 后端可控性
- 安全性
- 与现有手动模式的兼容
