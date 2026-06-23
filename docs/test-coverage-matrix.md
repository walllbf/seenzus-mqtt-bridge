# SavanAI Bridge 测试覆盖对照表

本文档整理当前插件内的自动化测试覆盖范围、每个测试验证的行为，以及测试失败时最可能代表的问题。

当前测试总数：以仓库实际 pytest 收集结果为准

建议执行命令：

```text
.\.venv-test\Scripts\python -m pytest tests test_bridge_retained_topics.py test_entity_filters.py -q
```

---

## 测试对照表

| 测试文件 | 测试名 | 主要验证点 | 失败通常代表什么 |
|---|---|---|---|
| `tests/test_tooling_smoke.py` | `test_pytest_config_exists` | `pytest.ini` 和 `requirements_test.txt` 存在 | 测试入口缺失，仓库无法按约定方式运行自动化测试 |
| `tests/test_helpers.py` | `test_fake_mqtt_client_records_publish_calls` | Fake MQTT client 能记录 `topic/payload/qos/retain` | 测试桩不可信，后续 MQTT 行为测试可能全部失真 |
| `tests/test_coordinator_behavior.py` | `test_publish_state_from_event_ignores_bridge_internal_entity` | 内部诊断实体不会被镜像到 `state` | 可能重新引入 `sensor.savanai_bridge_*` 自激循环 |
| `tests/test_coordinator_behavior.py` | `test_publish_state_from_event_publishes_regular_entity_state` | 普通实体状态变化会发布到正确 `state` topic | 真实设备状态事件可能发不出去，或 topic/payload 被改坏 |
| `tests/test_coordinator_behavior.py` | `test_prepare_for_reload_clears_old_retained_presence_when_bridge_changes` | 桥 identity 变化时会清掉旧 retained `presence` | 修改 `bridge_id` / `topic_root` 后 broker 残留旧桥 |
| `tests/test_reload_entry_behavior.py` | `test_async_reload_entry_clears_retained_presence_before_reloading` | `_async_reload_entry()` 先清 retained 再 reload | reload 时序错误，可能导致旧 retained 未清理 |
| `tests/test_mqtt_loop_behavior.py` | `test_loop_publishes_startup_snapshot_once_across_reconnect_cycles` | 每次（重）连都重发 retained `catalog`（`source=reconnect`/qos1）；全量 state 快照仍仅一次 | broker retained 丢失后若桥不在重连时补发 catalog → 后端 catalog 空 → live 全标不可读 + 控制 `entityUnavailable`，且不自愈直到插件 reload |
| `test_bridge_retained_topics.py` | `test_clears_previous_retained_topics_when_bridge_identity_changes` | topic 层正确判定“桥变了，需要清理旧 retained” | retained 清理判定规则失效 |
| `test_bridge_retained_topics.py` | `test_keeps_retained_topics_when_bridge_identity_is_unchanged` | 桥 identity 不变时不误清理 | 普通保存配置时可能误删当前桥 `presence` |
| `test_entity_filters.py` | `test_detects_internal_bridge_metric_sensor` | 内部过滤器能识别桥自己的诊断实体 | 自家状态实体可能重新漏进 `state` |
| `test_entity_filters.py` | `test_does_not_match_regular_entity` | 普通设备实体不会被误过滤 | 真实设备状态可能被错误丢弃 |
| `tests/test_config_flow_behavior.py` | `test_flatten_form_input_merges_section_values` | 折叠 section 提交后能正确 flatten 成配置数据 | 单页折叠配置提交后字段可能丢失 |
| `tests/test_config_flow_behavior.py` | `test_validate_requires_mqtt_host_in_manual_mode` | 手动模式缺少 `mqtt_host` 时会返回校验错误 | 配置流可能允许无效配置进入运行时 |
| `tests/test_config_flow_behavior.py` | `test_validate_requires_pairing_api_base_in_seamless_mode` | 快速配对模式缺少 `pairing_api_base` 时会返回校验错误 | 默认快速配对模式下可能允许空 Seenzus 地址进入外部跳转流程 |
| `tests/test_config_flow_behavior.py` | `test_validate_rejects_invalid_pairing_api_base_when_seamless_mode` | 快速配对模式会拒绝非法 Seenzus API 地址 | 插件可能把错误地址拿去创建 quick pair session |
| `tests/test_config_flow_behavior.py` | `test_validate_accepts_local_http_pairing_api_base_when_seamless_mode` | 本地 `http://IP:port/api` 形式的 Seenzus 地址可以通过校验 | 局域网后端地址会被错误拦截 |
| `tests/test_config_flow_behavior.py` | `test_mode_schema_only_shows_pairing_mode` | 第一步模式选择页只显示 `pairing_mode` | 配置流入口会混入后续字段，无法形成真正两段式流程 |
| `tests/test_config_flow_behavior.py` | `test_schema_shows_only_seamless_fields_in_seamless_step` | 快速配对步骤只渲染对应字段 | 配置页会同时混出手动字段，影响可用性 |
| `tests/test_config_flow_behavior.py` | `test_schema_shows_only_manual_fields_in_manual_step` | 手动配置步骤只渲染手动配对字段 | 切到手动模式后仍显示错误字段集合 |
| `tests/test_config_flow_behavior.py` | `test_user_step_shows_mode_selection_form` | 首配入口先展示模式选择页 | 无法通过两段式方式解决字段切换问题 |
| `tests/test_config_flow_behavior.py` | `test_user_step_routes_to_seamless_form` | 选择快速配对后会进入专属表单 | 模式选择后仍停留在错误页面 |
| `tests/test_config_flow_behavior.py` | `test_user_step_routes_to_manual_form` | 选择手动配置后会进入专属表单 | 手动模式入口无法正确跳转 |
| `tests/test_config_flow_behavior.py` | `test_seamless_step_starts_external_quick_pair` | 快速配对步骤会先创建 session 并进入外部授权跳转 | quick pair 可能仍停留在本地表单，无法发起外部授权 |
| `tests/test_config_flow_behavior.py` | `test_seamless_finish_creates_entry_with_bootstrapped_mqtt` | 外部授权完成后会交换 callback code 并把 MQTT 配置写入 entry | quick pair 可能拿不到自动桥接配置，仍要求用户手填 MQTT |
| `tests/test_config_flow_behavior.py` | `test_options_init_shows_mode_selection_form` | 已配置后的 options flow 也会先展示模式选择页 | 修改配置时仍无法真正切换模式 |
| `tests/test_config_flow_behavior.py` | `test_options_flow_creates_entry_with_flattened_data` | 已配置后的手动步骤会保存 flatten 后的数据 | 修改配置后 options 数据结构可能异常 |




| `tests/test_pairing_bootstrap.py` | `test_create_pairing_session_posts_expected_payload` | quick pair 会向 web-pairing session 端点创建外部配对会话 | 外部跳转前的 session 创建可能打错接口或拿不到 authorizeUrl |
| `tests/test_pairing_bootstrap.py` | `test_exchange_web_pairing_callback_code_posts_expected_payload` | callback exchange 会向正确端点发送 `sessionId/code/state` | 插件可能无法在授权回跳后换取 MQTT 配置 |
| `tests/test_pairing_bootstrap.py` | `test_fetch_web_pairing_session_status_reads_backend_status` | web-pairing status 查询会读取 `confirmed/bound/confirmedAt` 等字段 | 快速配对无法正确感知云端确认结果 |
| `tests/test_dispatch_behavior.py` | `test_dispatch_get_config_returns_hass_config` | `GET /api/config` 返回 HA config 内容 | `dispatch` 的基础 GET 映射可能被改坏 |
| `tests/test_dispatch_behavior.py` | `test_dispatch_get_state_returns_entity_and_touched_entity` | `GET /api/states/{entity}` 返回状态并标记 touched entity | 单实体状态拉取与后续补发 `state` 链路可能失效 |
| `tests/test_dispatch_behavior.py` | `test_dispatch_service_call_invokes_service_and_extracts_entities` | `POST /api/services/...` 会调用服务并提取 `entity_id` | 命令执行成功但拿不到 `touched_entities`，导致不补发 `state` |
| `tests/test_dispatch_behavior.py` | `test_dispatch_unsupported_route_returns_404` | 不支持的路由会返回 404 | 非法请求可能返回错误状态码或错误信息 |
| `tests/test_command_behavior.py` | `test_handle_v2_command_invalid_json_returns_400_result` | 非法 JSON 会回 400 `result` | 输入校验失效，坏 payload 可能造成异常或无响应 |
| `tests/test_command_behavior.py` | `test_handle_v2_command_publishes_result_and_followup_state` | 合法命令会先发 `result`，再补发对应 `state` | 主链路 `command -> result -> state` 可能被改坏 |
| `tests/test_runtime_flags.py` | `test_async_start_registers_state_listener_when_enabled` | 开启 `enable_state_events` 时会注册状态监听 | 外部状态变化可能完全不上报 |
| `tests/test_runtime_flags.py` | `test_async_start_skips_state_listener_when_disabled` | 关闭开关时不会注册状态监听 | 关闭后仍继续推送状态，导致配置不生效 |
| `tests/test_runtime_flags.py` | `test_on_state_changed_ignores_events_when_state_push_disabled` | 开关关闭后即便收到事件也不会继续创建推送任务 | 配置开关逻辑可能只在启动时生效、运行中无保护 |
| `tests/test_runtime_flags.py` | `test_publish_presence_includes_expected_payload` | `presence` payload 包含 `bridgeId/status/version` 且 retain 为真 | presence 协议字段可能丢失或 retain 被改坏 |
| `tests/test_bridge_id_behavior.py` | `test_build_bridge_id_sanitizes_custom_value` | 自定义 `bridge_id` 会被规范化清洗 | topic 中可能出现非法或不稳定 ID |
| `tests/test_bridge_id_behavior.py` | `test_build_bridge_id_uses_entry_prefix_when_empty` | 空 `bridge_id` 时会稳定生成默认值 | 默认桥标识不稳定，导致 topic 漂移 |


| `tests/test_pairing_behavior.py` | `test_try_pairing_uses_bootstrapped_quick_pair_credentials_without_rebootstrap` | quick pair 已写入 entry 后，运行时会直接消费已落地的 MQTT 配置 | 插件重启或 reload 后可能重复创建配对会话，导致流程不稳定 |


| `tests/test_sensor_behavior.py` | `test_pairing_sensor_exposes_extended_pairing_attributes` | 配对状态实体会暴露 `pairing_mode/config_source/expires_at/bound_at/last_step/last_api_base` 等扩展字段 | 用户在 HA 里看不到无感配对的关键状态信息和接口调用进度 |

---

## 当前已覆盖的高价值区域

目前已覆盖以下高价值风险：

1. 配置流两段式模式选择、表单跳转与数据 flatten
2. `enable_state_events` 开关行为
3. `command -> result -> state` 主链路
4. `ha_dispatcher` 的核心 API 映射
5. `presence` payload 与 retain 语义
6. reload 前旧桥 retained 清理
7. 内部诊断实体过滤
8. `bridge_id` 规范化与默认生成
9. pairing 的关键触发条件与成功状态
10. web-pairing session / callback exchange 契约
11. 快速配对状态读取契约
12. 快速配对 `session -> authorize -> exchange -> bound` 基础链路
13. 配对状态实体扩展属性
14. 配对接口调用进度可观察性

---

## 仍可继续补充的测试点

这些点的优先级已经低于当前“高价值测试点”，但后续仍值得补：

### 1. `dispatch` 其余映射分支

- `GET /api`
- `GET /api/states`
- `POST /api/events/{event_type}`
- `POST /api/template`

这些分支目前未测，适合作为中价值补充。

### 2. `command` 异常分支

- `dispatch()` 抛异常时是否回 500
- `correlationId` 作为备用消息 ID 是否生效
- `body` 不是 dict 时是否被正确忽略

### 3. `presence` 关闭/停止行为

- `async_stop()` 在正常停机时是否发送 `offline`
- `_skip_offline_presence` 开启时是否真的抑制 reload 中的 `offline`

### 4. quick pair 失败路径

- callback exchange 失败时错误提示是否清晰
- `last_error` 是否写入快速配对失败原因
- web-pairing session 创建失败时是否中止流程
- status 返回 `expired / rejected` 时状态是否正确落地

### 5. 更完整的集成级测试

目前大部分测试仍使用轻量 fake `hass` / fake MQTT client。  
如果后续要更进一步，可以再引入更贴近 Home Assistant 运行时的 integration-style 测试，验证：

- config entry setup/unload 全流程
- sensor 实体注册结果
- update listener 真实触发 reload

---

## 结论

当前测试集已经把最近多次修改涉及的高风险区域基本保护住了，尤其是：

- 自激循环
- retained 残留
- reload 时序
- 状态事件开关
- 主链路 `command -> result -> state`

因此现在的测试状态已经从“只覆盖最近踩坑点”提升到“核心行为有成体系的护栏”。后续继续补测试时，可以优先转向中价值分支和更完整的集成级验证。
