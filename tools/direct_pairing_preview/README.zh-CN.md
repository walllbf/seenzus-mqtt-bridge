---
doc_type: delivery-acceptance
domains: [Home-Assistant, pairing, browser]
evidence: [tests/test_pairing_origin.py, tests/test_pairing_commit.py, tests/test_pairing_commit_contract.py, tools/direct_pairing_preview/origin-claim-browser-test.cjs]
---

# 来源识别与两阶段提交验证版 · 0.2.16b3

本版在 0.2.16b2（电脑浏览器来源识别，已在 UAT 网页端验收）基础上增加两项：

- **两阶段提交**：服务端先签发候选凭证，HA 确认条目已保存到磁盘后才提交；取消、断网、重启或服务端拒绝后，HA 与应用两端回到同一个连接。
- **手机端来源识别**：Android、iOS、鸿蒙从应用发起时不依赖 `window.opener`，HA 页面经各环境的 `/ha-origin-claim/` 读取「刚从这里发起」的记录，自动进入发起环境。

包含 #77 基线 `b1cc89318d1a997a21603ba542a5d097dc38c33c` 的多环境及设备范围功能。#77 保持未合并。

## 前提

- 应用服务端需包含 seenzus PR #2241（`92b432e59059ad5e4d43bb554bacea558f828f8f`）。国内 PRD 与境外 PRD 已部署；**UAT 需部署同一版本后再验收**：本版自动识别会依次访问国内、境外、UAT 的认领页，任一可达环境仍是旧版时，手机端识别会停在该环境的页面。
- 未升级服务端的环境会退回旧的立即提交路径，不影响配对本身。
- Android、iOS 需要包含 PR #2241 的新安装包才会自动识别；旧安装包从 HA 手动选择环境。鸿蒙现有安装包即可识别，「浏览器未打开也提示已发起」的修复需新包。
- 手机能访问的 HA 地址需为 HTTPS，或局域网／本机地址（如 `192.168.x.x`、`*.local`）。其他 http 地址会直接提示手动选择环境。

## 安装

1. 在 HA 中创建完整备份，并额外保存当前 `custom_components/seenzus_bridge` 文件夹。
2. 将 ZIP 中的 `custom_components/seenzus_bridge` 整个文件夹覆盖到 HA 配置目录下的同名位置，包含 `translations`。不要嵌套成两层 `seenzus_bridge`。
3. 完整重启 HA，强制刷新 HA 页面。版本应为 `0.2.16b3`。

## 验收清单

两阶段提交与发起端无关，在电脑上完整验证一次即可；手机端验证来源识别与首次配对。

### 电脑网页（国内或境外 PRD 任选其一）

1. 在应用配对引导中点击「连接 Home Assistant」（不再需要专用入口），按提示打开自己的 HA，保持默认「快速配对（自动识别环境）」。
2. 应直接进入发起环境的 `/pair` 授权页；授权后回到 HA 完成创建。检查：HA 条目环境正确；应用中出现对应数据源，且在 HA 保存条目之后才显示配对成功；设备上线。
3. **授权后取消**：授权完成、HA 尚未创建条目时在 HA 取消。应用中不应新增数据源。
4. **重配时中断**：对已有条目重新配对，授权后、HA 保存前断网或重启 HA。恢复后两端一致：要么都是新连接，要么都是原连接，原连接不应离线。

### Android / iOS / 鸿蒙（国内 PRD 与境外 PRD 各一次）

1. 从 App 的配对引导发起。HA 应自动进入该 App 所在环境的授权页，全程不选择环境、不输入引导码。
2. 授权后检查条目环境、数据源与设备上线，同上。

### 直接从 HA 添加

不经应用发起时应出现手动选择环境；同一环境已有条目时提示重复，不创建第二个条目。

## 异常和回退

- 无法识别时显示「无法自动识别环境」并提供手动选择，不会自行切到国内或境外。
- 服务端拒绝提交时，HA 恢复服务端保留的连接并发出通知；新条目被拒绝时保留在 HA 中，需手动删除后重新配对。
- 回退插件时恢复原插件目录并重启。本版包含 #77 的条目版本 2；要回退到不支持版本 2 的旧正式版，应恢复安装前的完整 HA 备份。

## 已验证和待验收

| 范围 | 结果 |
| --- | --- |
| HA Core 2025.1.4 / Python 3.13 | 694 项测试通过 |
| HA Core 2026.10.2 / Python 3.14 | 694 项测试通过 |
| 跨仓库契约（真实应用配对路由，内存库） | 4 项通过：保存后才提交、取消重配保留原连接、HA 重启与服务端崩溃后的提交、拒绝后回滚 |
| 认领页链路，Chromium 与 WebKit | 各 10 项通过；页面与授权结果为测试夹具 |
| 用户实际 HA、真实账号授权、手机安装包 | 等待用户验收 |

浏览器夹具证明路由交接行为，不代表真实账号授权已经完成。

## 本地复现

使用安装了 HA 依赖的 Python 运行 `tools/direct_pairing_preview/export_browser_fixture.py fixture.json`，再用已有 Playwright 的 Node 环境运行 `tools/direct_pairing_preview/origin-claim-browser-test.cjs fixture.json <应用仓库>/client/public`（设置 `BROWSER=webkit` 切换内核）。跨仓库契约测试设置 `SEENZUS_APP_REPO=<应用仓库>` 后运行 `pytest tests/test_pairing_commit_contract.py`。
