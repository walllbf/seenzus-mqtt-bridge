# 交互式遥控 v1（静态待验证）

本副本基线 `0259e121275e5cf8da733024550579cdcbdb2989`，分支 `fix/ordered-interactive-remote`。对应应用 ADR0153。本轮外部代码执行被自动审批拒绝，主会话将工作收窄为静态编辑；此模块、导入、编译、pytest 和既有桥回归均**未运行**。以下是实现契约，不是测试通过或现场兼容声明。

presence 的 `capabilities.interactiveRemote` 为 `{version:1,profiles:["remote:tv_remote:3","remote:dangbei_local:2"],authorized:boolean}`。它只是能力声明，不能凭 retained 包创建有效会话。selected 现有 SAFE_SERVICES 不包含 remote/button，因此 authorized=false；本变更不扩权、不改授权模式。

请求使用现有 `command/{msgId}`，`method=POST,path=/api/seenzus/interactive-remote,body=...`。仅接受 MQTT 非 retained/QoS0 请求；云端发送时连接已就绪，MQTT.js `queueQoSZero=false`，不使用 outgoing store/重试。通用命令仍按原有 QoS。接收与执行结果使用现有 `result/{msgId}`，Result 可为 QoS1；重复结果不会再触发 HA。

所有 body 绑定 `principal`（World/住户派生 SHA256，不传原始用户 ID）、canonical `deviceId`、源生 `entityId`、`capabilityVersion`。源端实际串行以 HA Registry Device ID 为坐标，校验远程实体 registry ID/platform/config entry；不按 entityId 后缀或显示名猜动作。

- `open`：附随机 nonce、云端 sentAt。当前在线桥检查时钟、HA 当前授权、可用状态、reviewed manifest version（tv_remote 1.0.3 / dangbei_local 0.4.3），返回 nonce、sourceNow、随机 sessionId/generation/current epoch、30秒 expiresAt。云端验证 nonce/往返期限/时钟后才将句柄交给前台。未核实现场版本不能宣称支持。
- `press`：session 句柄、唯一 pressId、从1严格递增 sequence、已治理 action、云端重建的 service/data、云端 issuedAt/原始 expiresAt。服务端固定最多1500ms，原始浏览器事件时间只能缩短期限。源端保守预留100ms时钟误差，并以 wall/monotonic 双重期限在接收与执行前检查；不会在到达源端后重发完整TTL。
- `close`：只校验既有 session 的完整归属/连接代次，不依赖实体当前可用性，销毁其未执行项；已开始的 HA 调用仍保有物理栅栏直到结束。其它主体不能关闭该会话。
- `status`：绑定 session+pressId+sequence，读有限去重记录；实体暂时不可用也保留已知的拒绝结果，不执行/不重放。过期记录返回未知，不能制造物理成功。success=false包内仍须验证完整receipt/version/session/seq/expiry，不把任意错误对象当收据。

press 回执包含 `{version:1,sessionId,generation,epoch,expiresAt,pressId,sequence,phase,reason?}`。phase为 admitted / executed / rejected / unknown；expiresAt 是会话句柄字段，与逐键期限区分。admitted 才结束云端派发租约；executed 仅表示 HA blocking service 返回。晚到结果按 pressId 独立观察，HA service 返回不等于物理成功。

每物理设备6个等待项、全桥64个执行worker、128个保留会话、256条60秒去重记录、64个后台结果发布；每个共享物理栅栏至多7个使用者。会话30秒/最多256个序号；250ms回收定时器清除失效项。乱序/缺号/满载/过期/权限或profile变化/明确执行拒绝会清空该等待序列。新会话使旧等待失效，已有物理调用持锁完成；源断连/重载不恢复任何队列或旧epoch。新会话替换和 close 都不声称撤销已下发动作。

同一HA Device的普通旧 POST service（包括原电源/播放器入口）复用物理执行栅栏，防止绕过仍在执行的遥控调用；此串行化不证明未知TV switch与remote power功能等价。其它设备/读取维持原路径。HA调用完成后才释放物理栅栏，Result网络发布由独立有限观察任务承担。

每次云端接收前重读 canonical 权限；每次 HA 执行前复核源端可见权限/实体/profile/会话。桥无法立即得知 World 撤权，已接收等待项只受原短期限约束。前台失焦/隐藏/卸载/控件替换清除本地等待并尽力 close；隔离网络下不承诺 close 已送达。

第2轮补充当贝逐button角色：catalog entity可附`remoteCompanion={version:1,capabilityVersion:"remote:dangbei_local:2",remoteRegistryId,role}`。只在manifest0.4.3、registry platform/config entry domain、相同HA Device、唯一`("dangbei_local", nativeIdentifier)`及精确原生unique_id对应均成立时发布。remote原生unique_id须为`nativeIdentifier+"_remote"`；button仅匹配已核验的knownKey，且重复候选不发布。规范化role仅覆盖基础键和明确电源；reconnect/refresh_modes/settings/side及其它EXTRA保留。不传原始unique_id/nativeIdentifier，不按显示名或entityId后缀匹配。该字段仅为展示去重，不授权控制，也不落为物理state/history。旧桥/未知版本/歧义不声明角色，应用保留原按钮。

TV button与switch底层证据仍缺，不照搬当贝身份格式或删除电视按钮。电视mute/playpause为已审阅合法命令，当贝没有这些命令。桥测试 `tests/test_interactive_remote.py`、`tests/test_companion_roles.py` 已写但未执行；必须在获准的上游环境运行新测试及完整既有回归，并核验两现场安装版本/授权模式后再考虑部署或真机验收。
