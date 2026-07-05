"""开发者联调用的 MQTT 配对 API 地址覆盖。

正常安装（Release 下载 / HAOS）不含此文件，快速配对一律走
``const.DEFAULT_PAIRING_API_BASE``（内置生产地址）。仅当在 HA 配置目录放置
``<config>/seenzus_bridge_dev.json`` 时，配对才改用其中的 ``pairing_api_base``
连接本地联调后端。

文件住在配置目录（不在插件包内）：重装 / 升级插件 zip 不会丢，也不会被打进
发行包——因此联调包与上线包是逐字节同一份代码，靠这个文件区分环境。

fail-loud：文件不存在或未填地址时静默走生产默认（普通用户的常态）；文件存在
但内容非法（JSON 损坏 / 地址不是 http(s)）时抛 :class:`DevOverrideError` 拦住
配对——普通用户永远没有此文件、碰不到该报错，而开发者写错时会被当场拦下，
不会稀里糊涂把联调数据灌进生产。
"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

_LOGGER = logging.getLogger(__name__)

DEV_OVERRIDE_FILENAME = "seenzus_bridge_dev.json"
DEV_OVERRIDE_API_BASE_KEY = "pairing_api_base"


class DevOverrideError(Exception):
    """开发者覆盖文件存在但内容非法（fail-loud，拦住配对而非静默连生产）。"""


def _is_valid_api_base(value: str) -> bool:
    """API base 合法性：仅接受带 host 的 http(s) 地址（与原 UI 校验同一把尺）。"""
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _read_override_api_base(path: str) -> str | None:
    """同步读取覆盖文件，返回其中的 ``pairing_api_base`` 原文。

    在 executor 中调用（含阻塞文件 I/O）。缺文件 / 未填该键 / 空值返回 ``None``；
    文件存在但 JSON 损坏、顶层非对象、该键非字符串 → :class:`DevOverrideError`。
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return None
    except OSError as err:
        raise DevOverrideError(f"无法读取开发者覆盖文件 {path}: {err}") from err

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as err:
        raise DevOverrideError(f"开发者覆盖文件 JSON 解析失败: {err}") from err
    if not isinstance(payload, dict):
        raise DevOverrideError("开发者覆盖文件顶层必须是 JSON 对象")

    value = payload.get(DEV_OVERRIDE_API_BASE_KEY)
    if value is None:
        return None
    if not isinstance(value, str):
        raise DevOverrideError(f"{DEV_OVERRIDE_API_BASE_KEY} 必须是字符串")
    return value.strip() or None


async def resolve_dev_pairing_api_base(hass) -> str | None:
    """联调覆盖地址：合法则返回归一化后的 URL，否则返回 ``None``（调用方用默认）。

    文件存在但地址非法 → :class:`DevOverrideError`（fail-loud）。读文件放
    executor 避免阻塞事件循环；每次配对发起时新鲜读一次——删掉文件下次重配对
    立即生效，无需重启 HA。
    """
    path = hass.config.path(DEV_OVERRIDE_FILENAME)
    raw = await hass.async_add_executor_job(_read_override_api_base, path)
    if not raw:
        return None
    if not _is_valid_api_base(raw):
        raise DevOverrideError(
            f"{DEV_OVERRIDE_API_BASE_KEY} 不是合法的 http(s) 地址: {raw!r}"
        )
    # 仅联调场景应出现此日志；生产环境无此文件、永不触发。
    _LOGGER.warning("使用开发者覆盖的 seenzus 配对地址: %s", raw)
    return raw.rstrip("/")
