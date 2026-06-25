# SavanAI Bridge 用户手册（通俗版）

这份手册给普通用户使用，不需要编程基础。

---

## 1. 这个插件是做什么的

`SavanAI Bridge` 是一个装在 Home Assistant（HAOS）里的桥接插件。  
它的作用是：

- 让云端应用可以通过 MQTT 访问你家里的 HA 设备
- 不需要做内网穿透
- 支持“下发指令 + 返回执行结果 + 推送最新状态”

简单理解：它是“云端”和“你家 HA”之间的翻译官。

---

## 2. 安装方式

### 方式 A：通过 HACS 安装（推荐）

1. 打开 HA -> HACS -> Integrations
2. 添加自定义仓库（类型选 Integration）
3. 搜索 `SavanAI Bridge`
4. 安装后重启 HA

### 方式 B：手动安装（用打包好的 zip）

你现在可用的安装包路径是：

`dist/savanai_bridge_v3.0.8.zip`

手动安装步骤：

1. 解压 zip
2. 把里面的 `savanai_bridge` 文件夹复制到：
   `config/custom_components/`
3. 重启 HA
4. 在“设备与服务”里添加 `SavanAI Bridge`

---

## 3. 第一次配置怎么填

添加集成时，页面现在是“两段式模式分流”：

- 第一步先选 `快速配对（推荐）` 或 `手动配置（高级）`
- 第二步进入对应模式的配置表单
- 快速配对页只保留 `Seenzus API 地址`
- 选择快速配对后，会跳到外部 Seenzus 页面完成授权
- 授权成功后，浏览器会自动返回到 Home Assistant，本地插件会自动把 MQTT 桥接参数写好
- 手动配置页才会显示 `MQTT 连接参数`、手动配对参数和高级参数
- 保存后会自动重载集成，通常不用再手动重启 HA

可填写的字段包括：

- `MQTT Broker 地址`：你的 MQTT 服务器地址
- `MQTT 端口`：默认 `1883`
- `MQTT 用户名/密码`：按你的 Broker 账号填写
- `V2 Topic 根路径`：默认 `savant/v2`，一般不用改
- `Bridge ID`：留空会自动生成（推荐留空）
- `启用实体状态事件推送`：建议开启

如果你要用“快速配对”，只需要填写：

- `Seenzus API 地址`

如果你要改用“手动配对（高级）”，再填写：

- `Seenzus API 地址`



---

## 4. 配对模式怎么用

### 方式 A：快速配对（推荐）

1. 在插件里填写 `Seenzus API 地址`
2. 插件会自动创建配对会话，并跳转到外部 Seenzus 页面
3. 你在外部页面完成授权后，浏览器会自动回到 HA，插件会自动校验回跳并写入 MQTT 桥接配置
4. 创建 entry 后，插件会自动完成 MQTT 建桥并直接进入可用状态
5. 在 HA 的传感器属性里能看到：
   - `pairing_mode`
   - `config_source`
   - `pairing_status`
   - `pairing_session_id`
   - `pairing_expires_at`
   - `verification_code`
   - `pairing_bound_at`
   - `pairing_last_step`
   - `pairing_last_api_base`
6. quick pair 成功后，`pairing_status` 会直接进入 `bound`

### 方式 B：手动配对（兼容旧方式）

1. 切换 `pairing_mode` 为 `manual`
2. 展开手动配对参数
3. 填写 `Seenzus API 地址`
4. 保存后等待 MQTT 桥接连接成功

5. 插件连接 MQTT 后会直接进入可用状态

---

## 5. 日常怎么判断是否正常

插件会创建一个实体：`SavanAI Bridge 状态`

你可以重点看这些字段：

- `raw_status`：当前连接状态
- `request_count`：收到命令次数
- `result_count`：已返回结果次数
- `state_push_count`：已推送状态次数
- `error_count`：错误次数
- `last_error`：最后一次错误信息
- `pairing_mode`：当前是快速配对还是手动配对
- `config_source`：当前配置来自 `web_pair` 还是 `manual`
- `pairing_status`：当前配对阶段
- `pairing_session_id`：当前配对会话 ID
- `pairing_expires_at`：本次配对上下文过期时间
- `pairing_bound_at`：最终确认绑定时间
- `pairing_last_step`：最近一次执行到的配对步骤
- `pairing_last_api_base`：最近一次访问的配对后端地址

如果这几个计数持续增长，说明链路正常。

补充说明：

- `state` 是实时事件，不是历史缓存
- 插件自己的诊断实体不会再被重复上报到 MQTT
- 修改配置后，插件会自动重载；这时 `presence` 常见表现是先 `offline` 再 `online`
- 如果你改了 `bridge_id` 或 `topic_root`，旧桥遗留的 retained `presence` 会被自动清理

---

## 6. 常见问题（小白版）

### Q1：状态显示“发生错误”

先检查：

1. MQTT 地址、端口、账号密码是否正确
2. 你的 HA 主机能否访问到 MQTT Broker
3. `topic_root` 是否和云端一致

然后去 HA 日志搜索：`savanai_bridge`

### Q4：我怎么知道快速配对有没有完成

你可以看两处：

1. `SavanAI Bridge 配对状态` 实体属性
   - `pairing_last_step`
   - `pairing_last_api_base`
2. HA 日志里搜索 `savanai_bridge`
   - 会看到创建 web pairing session、外部授权完成、MQTT 配置写入、bridge 启动等步骤

### Q2：云端发了命令，但设备没变化

按顺序看：

1. `result` 是否返回 `success=true`
2. 有没有 `state` 状态事件推送
3. 下发的 `entity_id` 是否正确
4. 该设备是否在线、是否可控

### Q3：配对一直失败

重点检查：

1. 如果是快速配对，`Seenzus API 地址` 是否可访问，外部页面是否已完成授权
2. 如果是手动配对，`pairing_api_base` 是否可访问
3. 快速配对会话是否过期



---

## 7. 给普通用户的建议

- 不确定怎么填时，先用默认项（尤其 topic_root / bridge_id）
- 快速配对不需要手动填写 MQTT；只有手动配置模式才需要自己填 Broker 地址
- 配对成功后再接入自动化，不要一上来就复杂联动
- 修改配置后一般会自动生效；如果你同时改了桥标识，旧 retained `presence` 也会一并清理

---

## 8. 你现在已经有的文件

- 插件安装包：`dist/savanai_bridge_v3.0.8.zip`
- 本手册：`USER_MANUAL_zh-CN.md`
- 技术说明：`README.md`
- 测试覆盖对照表：`docs/test-coverage-matrix.md`
- 无感配对设计：`docs/seamless-pairing-design.zh-CN.md`
- 无感配对实施任务清单：`docs/seamless-pairing-implementation-plan.zh-CN.md`
- 快速配对流程说明：`docs/quick-pair-flow.zh-CN.md`

---

## 9. 最近更新

### 当前开发中变更（待发布）

- 快速配对改成了“授权成功后自动回到 HA 并自动完成收尾”的体验，不再依赖轮询完成
- 如果回跳状态不匹配、授权未完成或后端兑换失败，界面会给出更明确的错误提示
- quick pair 的测试已经补到 callback 成功、状态不匹配、code exchange 和 entry 自动创建

### v3.0.8

- 快速配对改为“填 Seenzus API 地址 -> 外部页面授权 -> 自动写入 MQTT”
- 快速配对页不再显示 MQTT 和高级参数，手动配置独立保留
- 配对状态实体新增 `config_source`，并补充 quick pair 运行步骤可观察性

### v3.0.7

- 配置页改为真正的“两段式模式选择”
- 快速配对支持本地 `http://IP:port` Seenzus API 地址
- 配对状态实体与日志新增 `pairing_last_step / pairing_last_api_base`

### v3.0.6

- 移除旧无感配对链路，只保留 web-pairing 快速配对
- 配置页改为“两段式模式选择”
- 配对状态实体新增 `pairing_mode / pairing_expires_at / pairing_bound_at`

### v3.0.5

- 配置页改成了单页折叠式，保存后自动生效
- 不会再把桥自己的诊断传感器反复推送到 MQTT
- 修改 `bridge_id` 或 `topic_root` 时，会自动清理旧桥残留的 retained `presence`

### 本轮无感配对实现

- 新增 `pairing_mode` 并区分快速配对和手动 MQTT 桥接
- 默认进入快速配对，手动配对收进折叠区
- 插件已接入 web-pairing callback exchange 快速配对链路
- 配对状态传感器新增 `pairing_mode / pairing_expires_at / pairing_bound_at`

### 工程维护更新

- 仓库已增加隔离测试环境和关键行为测试，用来持续验证状态推送、reload 和旧 retained 清理逻辑

### v3.0.4

- 修复了桥自身状态被反复上报的问题

### v3.0.3

- 修复了启动后可能出现的 MQTT 消息循环问题

### v3.0.2

- 增加了单页折叠配置和自动重载能力

