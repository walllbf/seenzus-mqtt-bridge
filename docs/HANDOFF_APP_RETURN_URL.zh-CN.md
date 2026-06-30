# Handoff：快速配对「回跳应用」——非插件改动清单

本文档覆盖**所有非插件侧的改动**，分为**后端**与 **App（前端）**两部分。插件侧已对接完成，不在此赘述。

**本期决定：授权完成后回跳到 App 的「集成列表页」。** 列表页只需「已连接」状态（绑定即可得），不依赖设备目录，因此**不需要新建落地页、也不存在设备数据就绪窗口期问题**。

涉及的三个既有接口（**无需新增接口**）：

- `POST /integrations/ha/web-pairing/session`
- `POST /integrations/ha/web-pairing/callback/exchange`
- `GET  /integrations/ha/web-pairing/session/{id}`

---

# Part 1 · 后端改动

## 1.1 新增响应字段 `appReturnUrl`

在以下响应 **payload 顶层**返回（用户绑定后要被带回的应用地址 = **集成列表页**）：

| 接口响应 | 要求 |
|---|---|
| `POST .../callback/exchange` | **必须** |
| `GET  .../session/{id}` | **推荐** |
| `POST .../session` | 可选（能早确定就返回） |

命名返回 `appReturnUrl`（插件兼容 `appReturnUri` / `returnUrl` / `returnUri`，优先 `appReturnUrl`）。未提供时插件保持现状（直接建 entry），天然灰度。

> 注：`appReturnMode` 字段已废弃（见 §1.2），后端**无需返回**；link 模式仅依赖本 `appReturnUrl`。

## 1.2 ~~`appReturnMode`~~（已废弃，无需返回）

早期为「redirect 自动回跳」预留的 `appReturnMode` 经评估在 HA config flow 架构上不可行，已从插件撤除（见插件 issue #1）。后端**不再返回** `appReturnMode`，插件也不读取；link 模式仅依赖 `appReturnUrl`。

## 1.3 取值来源与持久化

会话由插件发起，后端自行确定回跳目标，二选一：

- **方式 A（推荐）**：App 打开授权页时透传（如 `?appReturnUrl=...`），后端授权时**持久化到 session** 并回显。
- **方式 B**：后端按授权 client 从**服务端白名单**取固定地址。

无论哪种，**回显值必须来自服务端白名单**，禁止原样回显客户端任意 URL（见 §1.4）。

回跳目标 = **集成列表页**，取值建议（带 `sessionId`/`bridgeId` 便于列表页高亮新桥）：

- 原生 App：`seenzus://integrations?sessionId={id}&bridgeId={bid}`
- Web App：`https://app.seenzus.ai/integrations?sessionId={id}&bridgeId={bid}`

## 1.4 安全校验（必须实现）

`appReturnUrl` 等同 OAuth `redirect_uri`，必须防开放重定向 / 脚本注入：

1. **白名单**：只回显 scheme/host/path 前缀匹配已注册 App 的地址；不匹配则不返回该字段。
2. **禁危险 scheme**：`javascript:` / `data:` / `vbscript:` / `file:` / `blob:`。
3. **必须带 `//host` 授权部分，且不带 userinfo**：`http(s)://host…` 或 App deep link `seenzus://host…`。**不接受** opaque 形式（`mailto:`、`tel:`、`seenzus:done`）或带 `user@host` 的 URL（`@` 前缀会伪装真实跳转目标）——插件会拒绝。host 用域名（不支持 IPv6 字面量 `[::1]`）。
4. **字符约束**：必须是可打印 ASCII（`0x21`–`0x7E`）；非 ASCII 请先 percent-encode。不得含空格/控制字符/零宽字符，也不得含 `(` `)` `[` `]` `<` `>` `"` 反引号 `{` `}`（会破坏收尾页 markdown 链接或占位符替换）。
5. 方式 A 透传时，**先校验再持久化/回显**。

## 1.5 契约示例

`POST .../callback/exchange` 响应（新增字段，其余不变）：

```json
{
  "ok": true,
  "sessionId": "wps_abc123",
  "bridgeId": "ha-web-bridge",
  "configSource": "web_pair",
  "confirmedAt": "2026-04-20T12:01:22Z",
  "appReturnUrl": "seenzus://integrations?sessionId=wps_abc123&bridgeId=ha-web-bridge",
  "mqtt": { "host": "...", "port": 1883, "username": "...", "password": "...", "topicRoot": "seenzus/v2", "bridgeId": "ha-web-bridge" }
}
```

`GET .../session/{id}` 响应（新增 `appReturnUrl`）：

```json
{
  "ok": true,
  "status": "confirmed",
  "sessionId": "wps_abc123",
  "bound": true,
  "confirmedAt": "2026-04-20T12:01:22Z",
  "appReturnUrl": "seenzus://integrations?sessionId=wps_abc123&bridgeId=ha-web-bridge",
  "mqtt": { "...": "..." }
}
```

> 网关包裹形态（`{ data, code, message, isSuccess }`）支持：字段放进 `data` 内即可。

## 1.6（可选 · 仅将来要「深链直进设备流程」时才做）设备数据就绪信号

> 本期回集成列表页**不需要**此项。仅当以后想让回跳直接进入依赖设备数据的页面（设备列表/配置流程）时再补。

`bound=true` ≠ 设备数据就绪：绑定后桥才连 MQTT 发 `catalog`(startup_snapshot)，后端消费入库后设备库才有数据，中间有窗口期。届时在 `GET .../session/{id}` 暴露就绪位供 App 轮询：

| 字段 | 含义 | 来源 |
|---|---|---|
| `bound` | 会话已绑定 | `GET session` 的 `bound` |
| `catalogSynced` | 目录首次入库 | 收到 `catalog` → reconcile 落库 set-once |
| `deviceCount` | 该桥已入库设备数 | 按源 `COUNT(device_source_links)` |

放行进设备页条件 = **`bound && catalogSynced && deviceCount > 0`**（`deviceCount>0` 堵空/半截目录误就绪）。

> ⚠️ **不要用 `bridgeReady` / `pairingStatus=bridge_ready` 作就绪门**：经核 `coordinator.py`，web_pair 流程的 presence 只发 `pairing_status=BOUND`、**从不发 `bridge_ready`**（后者仅在 `pairing_mode==SEAMLESS && !=BOUND` 的另一路发）。照旧写法实现 = 永不就绪、空转到超时（后端 PR #309 已据此改对）。
>
> 后端 `GET session` 就绪字段已精简为 `{ catalogSynced, deviceCount }`（+ session 自带 `bound`），不再有 `bridgeOnline` / `bridgeReady`。MQTT 事件契约见 `docs/MQTT_BRIDGE_EVENTS_SPEC.zh-CN.md`（§5 catalog、§6 presence）。

## 1.7 后端测试清单

- [ ] `callback/exchange` 返回合法、过白名单的 `appReturnUrl`（指向集成列表页）
- [ ] `session/{id}` 返回 `appReturnUrl`
- [ ] 不在白名单 / 危险 scheme 的地址一律不回显
- [ ] 方式 A：客户端透传值经校验后才持久化/回显
- [ ] 未提供回跳地址时不返回该字段
- [ ] 网关包裹与非包裹两种形态都能携带新字段
- [ ]（就绪门，可选）`GET session` 返回 `{ catalogSynced, deviceCount }`，`deviceCount` 只在目录入库后 > 0；不返回 `bridgeReady`/`bridgeOnline`

---

# Part 2 · App（前端）改动

## 2.1（方式 A 时）打开授权页透传回跳地址

App 打开 Seenzus 授权页时附带 `?appReturnUrl=<集成列表页地址>`，由后端校验后持久化（见 §1.3）。

## 2.2 回跳目标 = 集成列表页（**无需新建页面**）

`appReturnUrl` 指向 App 已有的**集成列表页**，携带 `sessionId`/`bridgeId`。落地后：

- 展示该 Home Assistant 桥为「已连接 / 已绑定」（依据 `bound` / MQTT `presence`，**无需等设备目录**）。
- 可选：用 `sessionId`/`bridgeId` 高亮 / 滚动到刚绑定的这一条。

> 用户之后点进设备页时，`catalog` 早已同步完成，就绪窗口期被「用户点击间隔」自然吸收，无需额外处理。

## 2.3（可选 · 仅将来深链直进设备流程时）就绪门

若以后改为回跳后直接进入设备列表/配置流程，则该落地点需轮询 §1.6 的 `bound && catalogSynced && deviceCount > 0` 后再展示设备数据，并对未就绪给加载态 / 超时兜底。**本期回列表页不涉及。**

## 2.4 ~~redirect 自动回跳模式~~（已废弃）

redirect / 自动 302 已从插件撤除（HA config flow 架构不可行，见插件 issue #1）。App **无需**为其做任何适配；回跳一律走 link 模式（§2.2）。

## 2.5 App 测试清单

- [ ] 回跳后落到集成列表页，新桥显示为已连接
- [ ] 用 `sessionId`/`bridgeId` 能定位 / 高亮刚绑定的桥

## 2.6 link 模式收尾权衡：entry 搁浅 + 一次性凭证

收尾页会把用户接走，但 HA 的 entry 要等用户在收尾页点「提交」才建——HA 的 `async_create_entry` 会结束 flow，之后无法再展示带链接的页面（这正是 redirect 想绕开、被判不可行的根因，见插件 issue #1）。若用户点了返回链接离开、**没点提交**：

- entry 永不建；
- 后端已签发的**一次性 MQTT 密码无法重得** → 桥已绑定却连不上。

缓解：

1. **必做（插件已做）**：收尾页文案引导「**先点「提交」完成添加，再点返回链接**」，提交比链接更显眼，并提示「未提交就关向导需重新配对」。
2. **可选 · 跨仓（后端）**：提供**重配对恢复**路径——同桥重新配对时复签 MQTT 凭证，让「绑定了但 entry 没落」可自愈。后端当前无此口。
