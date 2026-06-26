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

## 1.2（仅自动回跳模式需要）新增 `appReturnMode`

payload 顶层返回 `appReturnMode: "redirect" | "link"`，缺省按 `link`。`redirect` 用于 App 单 webview 容器（插件自动 302 收尾）。只做手动链接收尾可不返回。

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
3. **必须带 `//host` 授权部分**：`http(s)://…` 或 App deep link `seenzus://…`。**不接受** opaque 形式（`mailto:`、`tel:`、`seenzus:done` 这种无 `//host` 的）——插件会拒绝。
4. **字符约束**：必须是可打印 ASCII（`0x21`–`0x7E`）；非 ASCII 字符请先 percent-encode。不得含空格/控制字符/零宽字符，也不得含 `(` `)` `[` `]` `<` `>` `"` 反引号（会破坏收尾页 markdown 链接）。
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
  "appReturnMode": "link",
  "mqtt": { "host": "...", "port": 1883, "username": "...", "password": "...", "topicRoot": "savant/v2", "bridgeId": "ha-web-bridge" }
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

`bound=true` ≠ 设备数据就绪：绑定后桥才连 MQTT 发 `catalog`(startup_snapshot)，后端消费入库后设备库才有数据，中间有窗口期。届时需在 `GET .../session/{id}` 暴露就绪位供 App 轮询：

| 字段 | 含义 | 来源 |
|---|---|---|
| `bridgeOnline` | 桥已连上 MQTT | `presence.status=online` |
| `bridgeReady` | 桥就绪 | `presence.pairingStatus=bridge_ready` |
| `catalogSynced` / `deviceCount` | 设备目录已入库 | 收到 `catalog`(startup_snapshot) 后置位 |

App 放行进设备页条件 = `bridgeReady && catalogSynced`。MQTT 事件契约见 `docs/MQTT_BRIDGE_EVENTS_SPEC.zh-CN.md`（§5 catalog、§6 presence）。

## 1.7 后端测试清单

- [ ] `callback/exchange` 返回合法、过白名单的 `appReturnUrl`（指向集成列表页）
- [ ] `session/{id}` 返回 `appReturnUrl`
- [ ] 不在白名单 / 危险 scheme 的地址一律不回显
- [ ] 方式 A：客户端透传值经校验后才持久化/回显
- [ ] 未提供回跳地址时不返回该字段
- [ ] 网关包裹与非包裹两种形态都能携带新字段
- [ ]（如启用 redirect 模式）返回 `appReturnMode` 且取值合法

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

若以后改为回跳后直接进入设备列表/配置流程，则该落地点需轮询 §1.6 的 `bridgeReady && catalogSynced` 后再展示设备数据，并对未就绪给加载态 / 超时兜底。**本期回列表页不涉及。**

## 2.4（redirect 自动回跳模式）配合点

若启用插件自动 302（`appReturnMode=redirect`）：App 需能接住该 deep link / universal link，落到集成列表页（§2.2）。

## 2.5 App 测试清单

- [ ] 回跳后落到集成列表页，新桥显示为已连接
- [ ] 用 `sessionId`/`bridgeId` 能定位 / 高亮刚绑定的桥
- [ ]（redirect 模式）能接住 deep link / universal link 并落到列表页
