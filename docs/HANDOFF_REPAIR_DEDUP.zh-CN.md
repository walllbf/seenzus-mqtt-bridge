# Handoff：HA 桥重复配对去重（消除僵尸桥）——后端改动清单

本文档描述**后端**需要做的改动，用于解决「同一个 HA 重新配对后，seenzus App 桥列表里多出同源僵尸桥」的问题。插件侧配合改动一并列出（可前向兼容先行）。

---

## 1 · 现象

用户对**同一个 HA** 重新配对（options 重配 / 删除条目后重加），seenzus App 的桥列表会**多出一条同源桥**，旧的变离线僵尸但仍留在列表。给 `bridgeName` 加家名只是让它们**可区分**，数量仍随每次重配增长。

## 2 · 根因（后端代码定位）

| # | 事实 | 位置 |
|---|---|---|
| 1 | `authorize()` 每次授权无条件新签 `bridge-${randomUUID()}` | `server/http/pairingSessions.ts:283` |
| 2 | `createSource` 去重键 = `(account_id, bridge_id)`（迁移 0012 unique，`onConflictDoNothing`）；随机 bridgeId 永不撞 → **每次都新建 source 行** | `server/db/pgRepository.ts:1820` |
| 3 | `bridgeName` 仅作展示名写入 source.name，**从不参与匹配/去重** | `server/http/pairingRoutes.ts:355` |
| 4 | `reclaimSweep` 只吊销**被删源**遗留的孤儿凭证；keep-set = 所有活 paired 源的 username，**不删除重复的活 source 行** → 僵尸桥不会被自动清理 | `server/agent/reclaim/reclaimSweep.ts:118` |

**结论**：系统缺一个**稳定的 HA 实例身份**，后端无从判断「这是同一个 HA 在重配」，只能每次当新桥建。`bridgeId` 是后端每次现签的随机值、`bridgeName` 是可变展示名，二者都不能当稳定身份。

## 3 · 方案：身份与展示分离，按 `(account, haInstanceId)` 去重

核心思想：**别再让 `bridgeId`（随机）/`bridgeName`（可变展示名）承担身份**，引入一个由插件提供、跨重配稳定的 HA 实例标识 `haInstanceId` 作为去重键。

### 3.1 插件侧（本仓库）——✅ 已实现（0.1.9）

`POST /integrations/ha/web-pairing/session` 请求体已新增字段：

```
"haInstanceId": "<HA 实例稳定 UUID>"
```

取值 = HA 官方 `homeassistant.helpers.instance_id.async_get(hass)`——**跨重启 / 重装集成 / 删除重加条目都稳定**，是「同一个 HA 安装」的权威标识。解析失败时**整字段省略**（不发 null），老后端忽略即可（天然灰度）。**后端无需等待插件，可直接按 §3.2 实现。**

### 3.2 后端侧改动

1. **接收 + 透传**：session body 读 `haInstanceId`（string，校验长度/字符），存进 session record（对位现有 `bridgeName`，见 `pairingSessions.ts:269`）；再沿 `authorize → binding → consumeCode → exchange` 一路透传（对位现有 `targetSiteId` 的透传方式）。

2. **加列 + 唯一约束**：sources 表加 `ha_install_id`（迁移）；对 `provisionMode='paired' AND providerType='home_assistant'` 建 **partial unique `(account_id, ha_install_id)`**（不影响非 paired / 非 HA 源，也不动现有 `(account_id, bridge_id)` 约束）。

3. **兑换时去重/顶替**：`createSource` 路径（`pairingRoutes.ts:352`）当 `(accountId, haInstanceId)` 命中已有源时，二选一：
   - **复用（推荐）**：沿用旧 `bridgeId`，重签该 bridgeId 的 EMQX 凭证（密码轮换），更新 `name`/`targetSiteId`。列表里始终**一条**，bridgeId 不变、设备关联（`device_source_links`）不断。
   - **顶替**：建新源后，对旧源调 `reclaimBridgeMirror`（吊旧凭证 + 清 retained）并删旧 source 行。
   两者都保证「一 (account, HA 安装) 一活桥」。

4. **多家仍正确**：不同 HA 安装 → 不同 `haInstanceId` → 各自独立桥（合法场景，**不去重**）。

5. **兼容/灰度**：`haInstanceId` 缺省（老插件）时保持现状（每次新建），不报错。

### 3.3 契约示例

`POST .../web-pairing/session`（新增 `haInstanceId`，其余不变）：

```json
{
  "bridgeName": "seenzus MQTT Bridge · 我的家",
  "haInstanceId": "0a1b2c3d-4e5f-6789-abcd-ef0123456789",
  "bridgeVersion": "0.1.9",
  "platform": "homeassistant",
  "haVersion": "2026.7.0",
  "redirectUri": "https://<ha>/api/seenzus_bridge/quick_pair/callback",
  "state": "<jwt>"
}
```

## 4 · 后端测试清单

- [ ] 同 `haInstanceId` 重配 → 只剩**一条**活 paired 源；旧凭证按策略被吊销或复用，无僵尸桥
- [ ] 不同 `haInstanceId` → **两条**独立源（多家不误并）
- [ ] 缺 `haInstanceId`（老插件）→ 保持现状（新建），不报错
- [ ] `haInstanceId` 超长 / 异常字符 → 校验拒绝或安全截断，不污染唯一约束
- [ ] 复用策略下：bridgeId 不变、`device_source_links` 关联不断、`name` 随新 `bridgeName` 更新

## 5 · 与现有契约的关系

纯**附加**，不改动现有 `appReturnUrl` / MQTT 配置返回契约（见 `HANDOFF_APP_RETURN_URL.zh-CN.md`）。`haInstanceId` 只用于配对去重，与回跳链接互不影响。

## 6 · 备注：为什么不能在插件侧单独解决

`authorize()` 无条件 `bridge-${randomUUID()}`（`pairingSessions.ts:283`），即使插件回传上次的 `bridgeId`，后端当前也会无视它另签新值。因此**必须后端配合**：要么接收 `haInstanceId` 去重（本方案），要么让 `authorize` 接受并复用插件回传的稳定标识。本方案选 `haInstanceId` 是因为它对「删条目重加」也稳定（旧 bridgeId 那时已随条目丢失）。
