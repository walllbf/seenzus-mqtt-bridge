"""开发入口预设，仅供新条目选择。

配置目录中的 seenzus_bridge_dev.json 提供 pairing_api_base 后，HA 添加流程显示
UAT / 开发预设。选定后地址保存在条目；修改或删除本文件不改动既有条目，重配对
始终使用原条目的环境。非法文件会阻止新配置，不回退到任何生产环境。
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
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not (parsed.username or parsed.password or parsed.query or parsed.fragment)


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
    只影响新条目的可选预设，无需重启 HA。
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
