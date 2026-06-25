# Seenzus OAuth式快速配对 Prompt 与迁移说明

## 前端 / 插件 Prompt

```text
你正在优化 Home Assistant 集成的快速配对体验，目标是尽量对齐 Xiaomi Home 的 OAuth 授权回跳体验。

当前要求：
1. quick pair 页面只保留 Seenzus API 地址
2. 提交后创建 web pairing session，并把 redirect_uri/state 一起传给后端
3. 外部授权成功后，浏览器直接回跳到 HA 本地 callback
4. 插件自动完成 state 校验、code exchange、entry 创建和 MQTT 桥接配置落地
5. 保留 manual 配置作为独立路径，不与 quick pair 混合
6. 错误态必须明确：callback 地址不可用、state 不匹配、授权失败、回跳超时、code exchange 失败、MQTT 缺失

请输出：
- 配置流交互步骤
- callback 生命周期
- state/code 校验点
- 需要改动的 config flow / options flow / 数据模型 / 测试点
- 与旧 external-step + polling 方案的兼容策略
```

## 后端 Prompt

```text
你正在把 Seenzus HA quick pair 从“外部页 + 轮询 session 状态”升级为类似 Xiaomi Home 的 OAuth callback 模式。

当前要求：
1. 创建 web pairing session 时支持 redirectUri 和 state
2. session 需要持久化 redirectUri/state，并在 complete 时按条件生成一次性短时 code
3. complete 在 callback 模式下返回 302 到 redirectUri?code=...&state=...
4. 提供 callback code exchange 接口，返回 bridgeId、mqtt、configSource、confirmedAt
5. code 必须一次性消费，且与 session/state 绑定校验
6. 保持原有不带 redirectUri 的 web pairing 客户端兼容

请输出：
- API 契约
- session/code/state 数据模型
- redirect 安全限制
- 需要新增或调整的路由
- 兼容旧 complete JSON 返回的策略
- 最小上线清单与测试清单
```

## 测试清单

- 插件：
  - 创建 quick pair session 时携带 `redirectUri/state`
  - callback 收到正确 `state` 时完成 `code exchange`
  - callback `state` 不匹配时阻止收尾
  - `code exchange` 成功后自动创建 entry 并落地 MQTT 配置
  - 保留旧 `web-pairing/session/{id}` 状态读取能力，便于灰度兼容

- 后端：
  - `web-pairing/session` 正常接受 `redirectUri/state`
  - `complete` 在 callback 模式下返回 302，非 callback 模式下继续返回 JSON
  - `callback/exchange` 成功返回 `bridgeId/mqtt/configSource/confirmedAt`
  - callback code 只能使用一次
  - callback state 不匹配时拒绝兑换

## 迁移策略

1. 先上线后端 `redirectUri/state/code exchange` 能力，并保持原轮询接口不变。
2. 插件切到 callback 主链路，但保留旧的 `fetch_web_pairing_session_status()` 代码路径作为短期兼容兜底。
3. 新旧 quick pair 会话可以共存：带 `redirectUri` 的走 callback，不带 `redirectUri` 的继续走原有 complete JSON 逻辑。
4. 观察稳定后，再决定是否下线纯轮询式 quick pair 收尾流程。
