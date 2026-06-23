# SavanAI Bridge 无感配对实施任务清单

本文档用于指导插件侧落地“无感配对”方案。  
目标是：在保留现有手动配置模式的前提下，新增以“二维码扫码 / 配对链接粘贴”为核心的快速配对流程。

关联设计文档：

- `docs/seamless-pairing-design.zh-CN.md`

---

## 1. 目标

插件侧需要完成以下能力：

- 新增“快速配对（推荐）”入口
- 手机端支持扫码填入配对链接
- PC 端支持直接粘贴配对链接
- 插件自动解析链接并调用 bootstrap 接口
- 自动 claim
- 自动轮询会话状态直到 `confirmed`
- 保留现有手动配置作为高级模式

---

## 2. 非目标

本阶段不做：

- 删除现有手动配置模式
- 改动 MQTT 主协议
- 在链接中直接承载最终长期凭证
- 直接依赖短码模式

---

## 3. 建议文件结构

### 新增文件

- `pairing_link.py`
  - 负责配对链接校验和解析
- `pairing_bootstrap.py`
  - 负责 bootstrap / status 请求
- `pairing_models.py`
  - 负责运行时配对数据结构
- `tests/test_pairing_link.py`
- `tests/test_pairing_bootstrap.py`
- `tests/test_pairing_config_flow.py`

### 修改文件

- `config_flow.py`
- `const.py`
- `__init__.py`
- `sensor.py`
- `README.md`
- `USER_MANUAL_zh-CN.md`
- `docs/test-coverage-matrix.md`

---

## 4. 分阶段任务

## 阶段 A：基础模型与链接解析

### A1. 新增配对模型

定义至少这些结构：

- `PairingLinkPayload`
- `PairingBootstrapResult`
- `PairingRuntimeState`

验收标准：

- 结构字段与设计文档一致
- 能被插件 coordinator/flow 复用

### A2. 新增配对链接解析模块

支持输入：

- HTTPS 配对链接
- 可选自定义 scheme 链接

必须校验：

- 域名 / scheme 白名单
- `session` 参数存在
- `nonce` 参数存在
- 非法链接直接报错

验收标准：

- 合法链接可解析为 `PairingLinkPayload`
- 非法链接能返回明确错误

---

## 阶段 B：配置流接入快速配对

### B1. 新增模式入口

配置入口增加：

- `快速配对（推荐）`
- `手动配置（高级）`

验收标准：

- 新用户默认进入快速配对
- 旧手动配置仍能进入并保存

### B2. 新增快速配对页

新增字段：

- `pairing_link`

手机端支持扫码填入，PC 支持粘贴。

验收标准：

- 链接输入后会先做本地格式校验
- 校验失败时 UI 有明确提示

### B3. 配对处理中状态展示

配置流增加处理中文案：

- 正在校验链接
- 正在申请配对凭证
- 正在连接 Seenzus
- 正在绑定 Home Assistant
- 等待云端确认

验收标准：

- 用户能知道当前卡在哪一步

---

## 阶段 C：bootstrap 与 claim

### C1. 实现 bootstrap 客户端

插件向固定后端入口调用：

- `POST /integrations/ha/pairing/bootstrap`

输出：

- `api_base`
- `pairing_token`
- `shared_secret`（可选）
- `expires_at`

验收标准：

- bootstrap 成功时可进入 claim
- bootstrap 失败时插件状态变为 `bootstrap_failed`

### C2. 与现有 claim 流程打通

让 bootstrap 获取的上下文直接接入现有：

- `_try_claim_pairing()`

验收标准：

- 无需用户手填 `api_base` / `pairing_token`
- claim 成功后状态变为 `claimed_pending_confirm`

---

## 阶段 D：会话状态轮询与最终绑定

### D1. 实现会话状态查询

插件在 claim 成功后开始轮询：

- `GET /integrations/ha/pairing/session/{sessionId}/status`

建议轮询节奏：

- 2s
- 3s
- 5s

直到：

- `confirmed`
- `expired`
- `rejected`

### D2. 新增绑定完成状态

插件收到 `confirmed` 后：

- 更新 `pairing_status = bound`
- 记录 `pairing_bound_at`
- 展示成功状态

验收标准：

- 云端确认后插件侧能稳定进入 `bound`

---

## 阶段 E：状态展示与实体增强

### E1. 配对状态实体增强

建议状态实体额外暴露：

- `pairing_mode`
- `pairing_session_id`
- `pairing_expires_at`
- `pairing_last_error`
- `pairing_bound_at`

### E2. 错误与过期提示

明确区分：

- 链接无效
- bootstrap 失败
- claim 失败
- 云端拒绝
- 配对过期

验收标准：

- 用户不需要看日志也能知道大致失败原因

---

## 阶段 F：兼容与迁移

### F1. 保留手动模式

现有字段继续可用：

- `pairing_api_base`
- `pairing_token`
- `pairing_code`
- `pairing_shared_secret`

### F2. 迁移策略

- 已有手动配置用户不强制迁移
- 新用户默认使用快速配对

验收标准：

- 不影响现有已配置实例运行

---

## 5. 测试任务

必须新增以下测试：

### 链接解析测试

- 合法 HTTPS 链接可解析
- 缺 `session` 返回错误
- 缺 `nonce` 返回错误
- 非白名单域名返回错误

### 配置流测试

- 快速配对模式入口存在
- 粘贴链接后正确保存到临时上下文
- 手动模式仍可提交

### bootstrap 测试

- bootstrap 成功返回临时配对上下文
- bootstrap 失败进入 `bootstrap_failed`

### claim / status 测试

- claim 成功进入 `claimed_pending_confirm`
- status 轮询到 `confirmed` 后进入 `bound`
- `expired` / `rejected` 时状态正确

### 回归测试

- 不影响当前 `command -> result -> state`
- 不影响 retained 清理逻辑

---

## 6. 验收标准

插件实现完成后，应满足：

1. 手机上打开 HA 配对页，可以扫码填入配对链接
2. PC 上打开 HA 配对页，可以直接粘贴配对链接
3. 用户不再需要手填 `Seenzus API 地址`
4. 插件能自动 bootstrap、claim、轮询绑定状态
5. 云端确认后插件进入 `bound`
6. 手动模式仍可正常使用
7. 自动化测试覆盖新增流程

---

## 7. 推荐实施顺序

1. 新增 `pairing_link.py` 和配对模型
2. 补链接解析测试
3. 接入快速配对配置流
4. 接入 bootstrap
5. 接入 claim
6. 接入 status 轮询
7. 增强配对状态实体
8. 更新 README / 用户手册 / 测试矩阵

---

## 8. 实施完成后的文档同步要求

每次变更后同步更新：

- `README.md`
- `USER_MANUAL_zh-CN.md`
- `docs/test-coverage-matrix.md`
- 本文档（如任务边界有变化）
