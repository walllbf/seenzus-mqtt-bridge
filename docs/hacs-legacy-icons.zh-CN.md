# seenzus 在旧 HACS / 极速版中的图标兼容

seenzus 自带本地图标。Home Assistant 2026.3 及以后，HA 集成页面可通过品牌 API 显示这些图片。

旧官方 HACS 及极速版 2.0.5.3 的商店列表仍使用旧公共品牌地址。插件增加随包前端兼容模块，在**添加并启用 seenzus 集成后**，将 HACS 页面里 seenzus 的旧图片地址替换为本机随包图片。用户无需运行脚本、上传图片或修改 HACS 文件。

## 使用

安装包含此功能的 seenzus 版本并启用集成；按 HACS 升级提示重启 HA，然后刷新 HACS。兼容模块加载后，约一秒内处理 seenzus 图标。浏览器或 App 保留旧页面时，关闭后重新打开 HACS，或强制刷新浏览器。

## 生效范围

- 展示名保持 `seenzus MQTT Bridge`。Python 类名中的 `Seenzus` 是类名约定，与品牌展示名不同。
- 只在 `/hacs` 页面处理 `<hacs-frontend>` 内、来自官方旧品牌地址或 Marketplace 品牌地址的 `seenzus_bridge` 图片。
- 支持旧 HACS 同源 iframe、多层开放 shadow DOM、延迟渲染和主题切换；透明底普通图也用于深色模式。
- 图片来自本机静态资源。JS 和 PNG 内容变化后资源版本随之变化，避免长期使用旧缓存。
- 其他集成、其他 HA 页面、现有本地品牌 API、访问令牌和 MQTT 数据不修改。本地图标加载失败时保留 HACS 原地址，不循环重试相同失败图片。
- 仅下载插件、尚未添加集成时，插件代码没有运行，无法改变旧 HACS 的商店图标。安装前的图标仍由 HACS / Marketplace 提供。
- 未来 HACS 改变组件名、使用跨域 iframe 或封闭 shadow DOM 时，可能需要继续适配；原生新版 HA / Marketplace 图标机制不受影响。

## 实现与生命周期

后端通过 HA 的 `async_register_static_paths` 和 `add_extra_js_url` 注册资源，不写入 HACS 安装目录，不修改 HACS Python 对象、JS 文件、浏览器原型或配置。

资源在 HA 组件初始化时注册一次，MQTT 条目重载不会重复注册。无前端时直接跳过。图片注册发生文件或运行时错误时只记录警告，不阻止 MQTT 集成启动。

浏览器模块在非 HACS 页面和隐藏页不扫描 DOM，缓存 HACS 根节点并限制外层页面扫描频率。页面离开、返回及重复加载资源时处理定时器，避免重复任务。静态资源与组件一样保留到本次 HA 进程结束，MQTT 条目卸载不影响已安装插件的图标。

## 验证

`tests/test_frontend_compat.py` 覆盖并发 / 重复注册、无前端、非致命失败及重试、内容缓存版本，并通过实际安装的 Core HTTP 服务读取随包 JS 和四张 PNG。

`tools/test_frontend_compat.mjs` 在隔离浏览器中加载实际随包模块，通过 HTTP、嵌套 shadow DOM 和同源 iframe 验证前端行为。CI 与 Python、HACS、Hassfest 检查一起运行。浏览器回放不等于用户实际 HA 页面验收。

早先提供的手动补丁包是独立的本机方案，不会被此模块自动执行。此实现不再需要用户应用那个补丁。

参考：[HA 本地图标说明](https://contributors.home-assistant.io/blog/2026/02/24/brands-proxy-api/)、[旧 HACS 图标改进的上游状态](https://github.com/hacs/frontend/pull/945)。
