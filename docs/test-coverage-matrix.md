# seenzus MQTT Bridge 测试覆盖对照表

> 现状对照（v0.1.9）。所有测试位于 `tests/` 目录，共 16 个文件。
> 运行：`python -m pytest tests -q`（或先建隔离环境，见 `README.md` 的「测试与验证」）。
> 本表由 `tests/` 实际收集重建；新增/重命名测试后请同步更新，或直接以 `pytest --collect-only` 为准。

## `tests/test_config_flow_behavior.py` — 配置流 / 快速配对 UI（41）

| 测试 | 验证行为 |
|---|---|
| `test_flatten_form_input_merges_section_values` | 分组 section 表单值会被展平合并 |
| `test_validate_requires_mqtt_host_in_manual_mode` | 手动模式缺 MQTT host 时校验报错 |
| `test_validate_allows_empty_pairing_api_base_in_seamless_mode` | 快速配对模式允许空 API 地址（回退默认生产地址） |
| `test_validate_rejects_invalid_pairing_api_base_when_seamless_mode` | 快速配对模式拒绝非法 API 地址 |
| `test_validate_accepts_local_http_pairing_api_base_when_seamless_mode` | 快速配对接受局域网 `http://IP:port` 地址 |
| `test_mode_schema_only_shows_pairing_mode` | 第一步只展示模式选择 |
| `test_schema_shows_pairing_api_base_in_seamless_step` | 快速配对步骤展示 API 地址字段 |
| `test_schema_shows_only_manual_fields_in_manual_step` | 手动步骤只展示手动字段 |
| `test_build_quick_pair_callback_context_uses_plugin_callback` | 回调上下文使用插件本地 callback 路径 |
| `test_quick_pair_callback_view_routes_options_flow` | callback 能路由到 options flow |
| `test_seamless_authorize_consumes_stored_callback_payload` | 授权步骤消费信箱中暂存的 callback payload |
| `test_user_step_shows_mode_selection_form` | 首步展示模式选择表单 |
| `test_user_step_routes_to_seamless_form` | 选快速配对进入对应表单 |
| `test_user_step_routes_to_manual_form` | 选手动进入对应表单 |
| `test_seamless_step_starts_external_quick_pair` | 快速配对触发外部授权步骤 |
| `test_seamless_authorize_exchanges_callback_code` | 回跳后用 code 兑换 MQTT 配置 |
| `test_seamless_authorize_rejects_mismatched_state` | state 不匹配时拒绝 |
| `test_seamless_authorize_does_not_raise_when_exchange_fails` | 兑换失败不抛异常、走错误提示 |
| `test_seamless_finish_creates_entry_with_bootstrapped_mqtt` | 收尾用引导得到的 MQTT 建 entry |
| `test_seamless_finish_creates_entry_with_web_pairing_mqtt` | 收尾用 web-pairing MQTT 建 entry |
| `test_options_init_shows_mode_selection_form` | options 流首步展示模式选择 |
| `test_options_flow_creates_entry_with_flattened_data` | options 流用展平数据建 entry |
| `test_options_seamless_step_uses_options_flow_manager` | options 的快速配对用 options flow manager 恢复 |
| `test_options_seamless_form_seeds_api_base_from_entry_data` | options 表单用现有 entry 的 API 地址回填 |
| `test_options_seamless_finish_creates_entry_with_empty_title` | options 收尾建 entry（空标题） |
| `test_seamless_finish_error_reshows_seamless_form_without_placeholder_support` | 老核缺 placeholder 时降级重显表单 |
| `test_seamless_finish_polls_legacy_status_until_bound` | 旧核走状态轮询直到 bound |
| `test_seamless_finish_reshows_form_when_session_never_bound` | 会话始终未 bound 时重显表单 |
| `test_quick_pair_callback_view_rejects_missing_state` | callback 缺 state 返回 400 |
| `test_quick_pair_callback_view_returns_202_when_flow_resume_fails` | flow 恢复失败时 callback 返回 202 |
| `test_quick_pair_callback_mailbox_evicts_oldest_beyond_cap` | payload 信箱超上限先逐最旧 |
| `test_record_quick_pair_diagnostic_creates_persistent_notification` | 配对失败写持久化诊断通知 |
| `test_notify_app_return_creates_notification_and_clears_diagnostic` | 返回链接通知创建并清失败诊断 |
| `test_backend_bridge_name_appends_home_name` | 桥名附加 HA 家名 |
| `test_backend_bridge_name_falls_back_when_no_home_name` | 无家名/等于裸名时回退裸名 |
| `test_backend_bridge_name_sanitizes_home_name` | 家名去控制字符 + 截断超长，保留中文/emoji |
| `test_clear_quick_pair_notifications_dismisses_both` | 无链接成功时清两条通知 |
| `test_sanitize_app_return_url` | 返回链接 URL 净化（scheme/host/危险字符） |
| `test_read_app_return_url_accepts_key_aliases` | 兼容 appReturnUrl/appReturnUri/returnUrl/returnUri |
| `test_seamless_captures_app_return_url_from_session` | 从 session 捕获返回链接 |
| `test_seamless_finish_creates_entry_with_return_link` | 成功页附返回链接 + 通知 |

## `tests/test_pairing_bootstrap.py` — 配对 HTTP 客户端 / 脱敏（12）

| 测试 | 验证行为 |
|---|---|
| `test_create_web_pairing_session_posts_expected_payload` | 创建会话 POST 载荷正确（含 `haInstanceId`） |
| `test_create_web_pairing_session_omits_ha_instance_id_when_absent` | 无实例 id 时省略该字段（不发 null） |
| `test_create_web_pairing_session_accepts_gateway_wrapped_response` | 兼容网关包裹响应 |
| `test_create_web_pairing_session_returns_diagnostics_on_backend_error` | 后端错误时返回诊断信息 |
| `test_fetch_web_pairing_session_status_reads_backend_status` | 读取会话状态 |
| `test_exchange_web_pairing_callback_code_posts_expected_payload` | 兑换 code POST 载荷正确 |
| `test_response_summary_redacts_mqtt_password` | 响应摘要脱敏 MQTT 密码 |
| `test_pairing_logs_never_carry_mqtt_password` | 配对日志绝不含明文密码 |
| `test_response_summary_masks_object_and_array_secret_values` | 对象/数组内密钥脱敏 |
| `test_response_summary_masks_2level_nested_object_secret` | 二级嵌套对象密钥脱敏 |
| `test_response_summary_masks_array_of_objects_secret` | 对象数组内密钥脱敏 |
| `test_failure_message_and_error_code_are_redacted` | 失败消息/错误码脱敏 |

## `tests/test_pairing_behavior.py` — 配对绑定语义（3）

| 测试 | 验证行为 |
|---|---|
| `test_manual_pairing_does_not_call_backend_pairing` | 手动配对不调后端配对接口 |
| `test_try_pairing_marks_bound_for_web_pair_source` | web_pair 源标记为 bound |
| `test_try_pairing_waits_when_seamless_config_is_not_web_pair` | 非 web_pair 的 seamless 配置时等待 |

## `tests/test_coordinator_behavior.py` — 协调器 / 状态 / 设备目录（20）

| 测试 | 验证行为 |
|---|---|
| `test_fire_notifies_listeners_without_async_add_job` | 无 async_add_job 时仍通知监听器 |
| `test_mqtt_auth_error_sets_pairing_status_for_web_pair_config` | MQTT 认证失败置对应 pairing_status |
| `test_presence_includes_mqtt_and_pairing_diagnostics` | presence 含 MQTT + 配对诊断 |
| `test_publish_state_from_event_ignores_bridge_internal_entity` | 忽略桥自身诊断实体 |
| `test_state_publish_failure_counts_one_error_with_state_publish_failed_label` | 状态发布失败只记一次错误 |
| `test_publish_state_from_event_publishes_regular_entity_state` | 普通实体状态正常发布 |
| `test_publish_state_from_event_ignores_model_marked_entity` | 忽略名称带 `*` 的型号标注实体 |
| `test_get_states_command_skips_model_marked_entity` | 全量状态跳过型号标注实体 |
| `test_get_states_command_publishes_full_state_snapshot` | 发布全量状态快照 |
| `test_publish_device_catalog_groups_entities_under_devices` | catalog 按设备聚合实体 |
| `test_device_catalog_reports_entity_category_config` | catalog 上报 entityCategory=config |
| `test_device_catalog_reports_entity_category_diagnostic` | catalog 上报 entityCategory=diagnostic |
| `test_device_catalog_omits_entity_category_for_primary_controls` | 主控实体不带 entityCategory |
| `test_device_catalog_reports_partial_availability` | 上报部分可用性 |
| `test_device_catalog_primary_offline_with_live_secondary` | 主控离线但次实体在线的判定 |
| `test_device_catalog_primary_available_aggregates_multiple_primary_entities` | 多主控实体可用性聚合 |
| `test_device_catalog_keeps_ha_device_domain_entities` | 保留 HA device registry 归属实体 |
| `test_device_catalog_excludes_model_marked_entities` | 排除型号标注实体 |
| `test_device_catalog_command_publishes_catalog_snapshot` | catalog 命令发布快照 |
| `test_prepare_for_reload_clears_old_retained_presence_when_bridge_changes` | 桥标识变更时清旧 retained presence |

## `tests/test_dispatch_behavior.py` — HA 内部 API 分发 / 安全策略（10）

| 测试 | 验证行为 |
|---|---|
| `test_dispatch_get_config_returns_hass_config` | `GET /api/config` 返回配置 |
| `test_dispatch_get_state_returns_entity_and_touched_entity` | 取单实体状态 |
| `test_dispatch_service_call_invokes_service_and_extracts_entities` | 服务调用并抽取受影响实体 |
| `test_dispatch_unsupported_route_returns_404` | 不支持路由返回 404 |
| `test_dispatch_config_redacts_location_by_default` | 默认裁剪家庭经纬度等敏感字段 |
| `test_dispatch_config_full_when_policy_allows` | 策略放开后返回完整 config |
| `test_dispatch_dangerous_service_blocked_by_default` | 默认拦截危险服务 |
| `test_dispatch_dangerous_service_allowed_with_policy` | 策略放开后允许危险服务 |
| `test_dispatch_homeassistant_restart_blocked_but_turn_on_allowed` | 拦截 restart 但放行普通控制 |
| `test_dispatch_template_disabled_by_default` | 默认禁用模板渲染 API |

## `tests/test_command_behavior.py` — 命令通道 / msgId（8）

| 测试 | 验证行为 |
|---|---|
| `test_handle_v2_command_invalid_json_returns_400_result` | 非法 JSON 命令返回 400 结果 |
| `test_handle_v2_command_publishes_result_and_followup_state` | 命令发布 result 并跟随 state |
| `test_msgid_precedence_payload_msgid_wins_over_correlation_and_topic` | msgId 优先级：payload > correlation > topic |
| `test_msgid_precedence_correlation_id_wins_over_topic_segment` | correlation 优先于 topic 段 |
| `test_msgid_falls_back_to_topic_segment_when_payload_has_no_ids` | 无 id 时回退 topic 段 |
| `test_full_snapshot_states_use_qos0_while_result_uses_qos1` | 全量快照 QoS0、result QoS1 |
| `test_publish_result_failure_counts_error_once_and_does_not_raise` | result 发布失败只记一次错误、不抛 |
| `test_last_req_is_timezone_aware_after_command` | 命令后 last_req 带时区 |

## `tests/test_mqtt_loop_behavior.py` — MQTT 连接生命周期（5）

| 测试 | 验证行为 |
|---|---|
| `test_loop_missing_host_marks_error_and_waits_for_external_auth` | 缺 host 时置错误并等外部授权 |
| `test_loop_happy_connect_subscribes_then_presence_snapshot_catalog` | 连上后订阅 → presence → 快照 → catalog |
| `test_loop_publishes_startup_snapshot_once_across_reconnect_cycles` | 重连周期内启动快照只发一次 |
| `test_loop_defers_startup_snapshot_until_ha_started` | 启动快照等 HA started 后再发 |
| `test_loop_routes_command_message_to_published_result` | 命令消息路由到 result |

## `tests/test_runtime_flags.py` — 运行时开关 / presence 心跳（5）

| 测试 | 验证行为 |
|---|---|
| `test_async_start_registers_state_listener_when_enabled` | 开启时注册状态监听 |
| `test_async_start_skips_state_listener_when_disabled` | 关闭时不注册 |
| `test_on_state_changed_ignores_events_when_state_push_disabled` | 关闭状态推送时忽略事件 |
| `test_publish_presence_includes_expected_payload` | presence 载荷正确 |
| `test_presence_heartbeat_publishes_every_default_interval` | presence 按默认间隔心跳 |

## `tests/test_bridge_protocol_behavior.py` — retained 清理 / 实体过滤（7）

| 测试 | 验证行为 |
|---|---|
| `test_clears_previous_retained_topics_when_bridge_identity_changes` | 桥标识变更清旧 retained topic |
| `test_keeps_retained_topics_when_bridge_identity_is_unchanged` | 标识不变则保留 retained |
| `test_detects_internal_bridge_metric_sensor` | 识别桥自身诊断实体 |
| `test_does_not_match_regular_entity` | 不误判普通实体 |
| `test_name_with_asterisk_is_model_marked` | 名称带 `*` 判为型号标注 |
| `test_plain_name_is_not_model_marked` | 普通名称不判型号标注 |
| `test_missing_name_is_not_model_marked` | 无名称不判型号标注 |

## `tests/test_bridge_id_behavior.py` — bridgeId 生成（2）

| 测试 | 验证行为 |
|---|---|
| `test_build_bridge_id_sanitizes_custom_value` | 自定义 bridgeId 净化 |
| `test_build_bridge_id_uses_entry_prefix_when_empty` | 留空时用 entry 前缀生成稳定 id |

## `tests/test_sensor_behavior.py` — 传感器（4）

| 测试 | 验证行为 |
|---|---|
| `test_pairing_sensor_exposes_extended_pairing_attributes` | 配对状态传感器暴露扩展属性 |
| `test_status_sensor_pins_identity_attributes_and_device_info` | 状态传感器固定身份属性 + device_info |
| `test_metric_sensor_maps_key_to_coordinator_counter` | 指标传感器映射到协调器计数 |
| `test_sensors_render_sanitized_bridge_id_matching_topics` | 传感器展示的 bridgeId 与 topic 一致 |

## `tests/test_reload_entry_behavior.py` — 重载（1）

| 测试 | 验证行为 |
|---|---|
| `test_async_reload_entry_clears_retained_presence_before_reloading` | 重载前清 retained presence |

## `tests/test_tooling_smoke.py` — 工程结构冒烟（2）

| 测试 | 验证行为 |
|---|---|
| `test_pytest_config_exists` | pytest 配置存在 |
| `test_hacs_config_matches_custom_component_layout` | HACS 配置与集成目录布局一致 |

## `tests/test_helpers.py` — 测试桩自检（1）

| 测试 | 验证行为 |
|---|---|
| `test_fake_mqtt_client_records_publish_calls` | Fake MQTT 客户端记录 publish 调用 |
