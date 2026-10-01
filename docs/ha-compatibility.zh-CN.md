# HA 兼容性验证

最低支持版本为 HA Core 2025.1.4，不设置运行时版本上限。CI 用于发现真实接口变化；未进入历史测试矩阵不等于不能读取或控制设备，也不承诺所有未来版本和第三方集成都无条件兼容。本工作不涉及应用后端能力门或应用部署。

## 版本选择与证据

Validate 工作流保留 push、PR、每日定时和手动入口。每次运行先从 [HA 官方稳定通道](https://version.home-assistant.io/stable.json) 读取 `homeassistant.default`，解析出一次运行内固定的确切版本，再建立两条隔离作业：

- 最低版：HA 2025.1.4、Python 3.13。
- 最新稳定版：官方稳定通道返回的确切 Core tag，Python 来自同一 tag 的 `.python-version`。

两条作业从 PyPI 安装 `homeassistant==确切版本` 的官方发行包，避免每次从 Git 源码重建。pytest、pytest-asyncio、pytest-timeout 都读取对应 Core tag 的测试依赖精确版本。最低版发布时尚无 `.python-version`，因此保留既有的 Python 3.13 通道。最新通道不接受 beta、dev 或浮动分支。官方查询失败、元数据缺失、发行包不可用、依赖安装失败或测试失败均令 CI 失败，不回退到上次成功版本。

`ha-matrix` artifact 保存解析结果。每条版本作业保存实际 Python/Core 版本、全部已安装依赖和 JUnit 测试结果；安装失败时也尽量保留已完成的环境信息。测试通过 `EXPECTED_HA_VERSION` 检查真实导入的 Core 等于作业选择，缺失变量或不匹配都会失败。禁止从导入的版本自动填入预期值。

截至 2026-09-20，官方稳定通道解析为 2026.9.3，Python 为 3.14.5。这只是本次解析记录，不是未来矩阵的固定上限。

## 重现指定版本

GitHub Actions → Validate → Run workflow → `home_assistant` 填入 `2026.9.2`（或失败记录中的确切稳定版本）。此时运行最低版和指定版本，不查询当日最新版本。留空则恢复最低版＋最新稳定版。手动重现不会修改每日默认选择。

本地先解析所需环境：

```sh
python tools/ha_matrix.py --version 2026.9.2
```

使用输出中指定的 Python 创建独立虚拟环境，先安装该条目的 `core` 和 `test_dependencies`，再按该 Core 的 `homeassistant/package_constraints.txt` 安装 `requirements_contract.txt` 与集成 manifest 的实际依赖。完整安装步骤见 Validate 工作流。最后执行：

```sh
EXPECTED_HA_VERSION=2026.9.2 python -m pytest -q --junitxml=pytest.xml
```

PowerShell 使用 `$env:EXPECTED_HA_VERSION='2026.9.2'` 后运行同一 pytest 命令。最低版相应填 `2025.1.4`。历史 `requirements_test.txt` 的通用开发依赖不替代矩阵里的精确版本。最终复现环境以 CI Ubuntu 的安装清单为准；本地 Windows 如需额外 wheel 适配，必须披露，不能把适配后的依赖清单冒充原版安装。

## 测试边界

继续运行完整的 Publisher、配置、协调器、命令幂等、重连和启动生命周期测试，不为版本更新扩大设备控制权限。新增的真实 HA 契约在临时配置目录创建隔离 HomeAssistant 实例：

- 使用真实 Device/Entity/Area 注册表和状态机，验证设备绑定的未知 Domain、独立 Helper、实体类别、稳定标识、区域覆盖，以及 wire 版本与实际 Core 版本分别上报。
- 使用 HA 自带服务描述读取路径生成动作目录，注册及移除服务后核验目录内容，而非伪造服务描述返回值。
- 四类代表操作（开关、数值、选项、触发）经过真实 HA ServiceRegistry 和 schema 校验。模拟设备 handler 只更新隔离状态机，MQTT 使用内存传输。
- 缺失服务、schema 错误、设备 handler 异常必须给出失败结果；HA 接受命令但设备尚未变化时，回读仍保留原状态，不能凭接受就伪造确认。
- 保留 `unknown` 可用、`unavailable` 不可用的区别，验证真实状态时间及完整属性；实时、命令回显和快照的 `ts` 等于 HA `last_updated`，recorder 补录的 `ts` 等于原始 `last_changed`，不使用桥构造消息的当前时间。

真实接口测试不启动硬件集成，不连接住户 HA 或 MQTT。FakeHass 行为测试仍用于故障、并发和生命周期覆盖，但不能单独证明上游 API 兼容。当前官方 Entity Platform 集合取自安装中的 Core；“45 项”仅是 2026.8 的历史分类基线，不对未来数量作固定断言。

## WSS 传输与断线恢复（#62）

运行和契约依赖统一使用有上限的兼容范围，并遵守所选 Core 的运行时约束。已验证 HA 2025.1.4 的 aiomqtt 2.0.1 / Paho 1.6.1 与 HA 2026.9.4 的 aiomqtt 2.5.1 / Paho 2.1.0。WSS I/O guard 使用这些库的私有 socket/断线通知接口，升级依赖时需要重跑 `tests/test_mqtt_io_guard.py`：真实回环 TLS 背压下的 PING/CLOSE、接收恢复和异常通知、完整 WSS/MQTT 握手，以及小消息先获 QoS 1 确认的并发场景。TLS 证书仅为测试生成，临时目录在结束时删除。

`tests/test_mqtt_recovery.py` 使用真实协调器覆盖失败结果后停止发送、状态失败后停止回传错误、取消传递，以及重连等待前取消旧命令和 #61 新增的历史补录任务。其他正常命令与历史语义继续由完整套件覆盖。修复没有改变 broker 消息限制或固定重连间隔；这些回归通过不能代替用户 HA 的安装及首次断线原因验证。

### WSS 断线后的回调生命周期（#67）

Paho 在连接线程排队注册 writer 后，socket 可能先被关闭；aiomqtt 原始回调缓存的 fd 随后在 HA/Linux 注册时会触发 `Bad file descriptor`。连接等待 Future 已取消时，继续读取 EOF 又会使 aiomqtt 的断线回调在读取该 Future 的异常时抛出 `CancelledError`。guard 将 reader/writer 的安装与就绪回调绑定到原 socket，在事件循环执行时检查身份、有效 fd 和连接取消状态；取消后的 socket 使用 Paho 原生关闭路径清理。reader 安装仍启动 aiomqtt 的 keepalive 任务，关闭仍沿用原生取消路径。

真实 Paho 回归覆盖连接线程中的 writer 回调尚未返回、reader/writer 已排队但 socket 已关闭，以及连接取消后收到 EOF 的场景。真实 TLS/WSS 握手分别验证 Broker 的鉴权拒绝继续上报、调用者取消继续传播，并检查没有事件循环回调异常。旧版 Paho 的鉴权拒绝码为 5，新版映射为 135；修复没有放宽 Broker 权限，也不恢复已吊销的凭证。

## 已安装 MQTT 库的升级检查（#66）

HA 的已安装检查只匹配 manifest 中的单个版本范围，可能跳过安装器：例如 aiomqtt 2.5.1 / Paho 1.6.1 分别满足桥的范围，但不能一起导入。集成在全局 `async_setup` 中读取当前 Core 的约束文件，将其与 manifest 范围取交集，再通过 HA 的需求管理器处理；同时核验 aiomqtt 发行包声明的 Paho 依赖。旧版 Core 下若现有 aiomqtt 需要较新的 Paho，就排除该不兼容的 aiomqtt 版本，让 HA 安装器按 Core 约束重新求解。

修复沿用 HA 的安装锁、重试次数和失败记录，遵守跳过安装设置，安装后再次检查实际元数据。元数据检查在 HA 的共享安装锁下同时记录失败原因和当时的 aiomqtt 版本，释放锁后才请求安装器；其他集成若已完成修复，桥接受已兼容的组合，不会用旧检查结果排除新版本。若模块已在内存中保留旧版本，集成给出重启提示，不热替换其他集成正在使用的模块。

`tests/test_mqtt_requirements.py` 通过真实 HA 需求管理器复现缓存与已安装路径，覆盖旧版本残留、Core 升级和降级、混装、跳过安装、安装失败及已导入的旧模块，并用两个并发启动任务验证其他集成在检查后或安装前修好依赖的场景。CI 还在独立虚拟环境预装真实 aiomqtt 2.5.1 / Paho 1.6.1，运行 `tools/mqtt_upgrade_smoke.py`，确认 manifest 检查会接受该组合，随后由集成启动和真实 uv 安装器修复，并成功构造 TCP/WebSocket 客户端。该步骤不预先导入损坏的 MQTT 库，也不连接外部 broker；过程证据保存在 `mqtt-upgrade.txt`。

## 完成标准

最低版与实施时最新稳定版的完整测试通过，指定版本能够运行相同检查，并保留精确版本和依赖证据。如果发现接口变化，先固定真实失败用例，再做最小桥端修复并重跑两个通道。兼容性测试自身不能新增运行时版本上限，也不能改变配对、凭证、topic、身份、空间归属或幂等规则。

## #49 本地实施验证（2026-09-20）

以下结果来自基于主分支 `dfd34a1` 单独整理的 #49 PR 分支，不包含尚未合并的多环境配对工作。此前在多环境开发分支上的 287 项结果不作为此 PR 的测试总数。

| 实际 Core | 实际 Python | 完整测试 |
| --- | --- | --- |
| 2025.1.4 | 3.13.15 | 249 passed |
| 2026.9.2（指定重现） | 3.14.5 | 249 passed |
| 2026.9.3（当次最新稳定版） | 3.14.5 | 249 passed |

以上是 Windows 隔离环境、真实官方 Core 包的结果。最低版沿用既有本地 wheel 适配（ciso8601 2.3.3、lru-dict 1.4.1），两个 2026.9 环境按发行包原依赖安装，依赖一致性检查通过。三套环境均输出 JUnit 结果及完整依赖清单；这不替代提交后的 Ubuntu、Hassfest 和 HACS CI。

另验证了官方稳定通道及指定 2026.9.2 的实际解析、缺失/错误预期版本会失败，以及查询失败不回退的测试。版本解析脚本通过 mypy，工作流通过 actionlint（本地未安装 shellcheck，未运行其附加检查），Python 编译检查及差异空白检查通过。

真实新版 HA 要求在加载设备注册表前初始化它，ConfigEntry 构造增加空子条目参数；隔离测试按实际接口初始化。桥读取和调用的接口均未发现不兼容，本次未修改桥运行代码。规范与规格双线审查均无可操作发现。

## 合并前复审（2026-09-21）

更新至主分支 `8d045e4` 后，HA 2025.1.4、2026.9.2、2026.9.3 完整测试各为 **258 passed**。复审发现四类服务用例的旧属性等值断言不接受 #51 新增的 `seenzus_display`；已改为同时验证原始名称保留、展示时区正确和 HA 原始属性未被改写。未回退展示元数据功能，也未修改桥运行代码。
