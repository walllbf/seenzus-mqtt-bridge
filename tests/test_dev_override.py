from __future__ import annotations

from types import SimpleNamespace

import pytest

from seenzus_bridge.dev_override import (
    DEV_OVERRIDE_FILENAME,
    DevOverrideError,
    _read_override_api_base,
    resolve_dev_pairing_api_base,
)


def _fake_hass(config_dir):
    """最小 hass 替身：config.path 拼路径 + async_add_executor_job 同步执行。"""

    async def _run(func, *args):
        return func(*args)

    return SimpleNamespace(
        config=SimpleNamespace(path=lambda name: str(config_dir / name)),
        async_add_executor_job=_run,
    )


def test_read_missing_file_returns_none(tmp_path) -> None:
    # 普通用户的常态：没有覆盖文件 → None（调用方用生产默认，无报错）。
    assert _read_override_api_base(str(tmp_path / DEV_OVERRIDE_FILENAME)) is None


def test_read_valid_file_returns_raw_value(tmp_path) -> None:
    path = tmp_path / DEV_OVERRIDE_FILENAME
    path.write_text('{"pairing_api_base": "http://192.168.9.99:5078/api"}', encoding="utf-8")
    assert _read_override_api_base(str(path)) == "http://192.168.9.99:5078/api"


def test_read_empty_or_missing_key_returns_none(tmp_path) -> None:
    # 文件建了但没填 / 空串 → 不算覆盖，走默认（静默）。
    for content in ('{}', '{"pairing_api_base": ""}', '{"pairing_api_base": "   "}'):
        path = tmp_path / DEV_OVERRIDE_FILENAME
        path.write_text(content, encoding="utf-8")
        assert _read_override_api_base(str(path)) is None


def test_read_malformed_json_raises(tmp_path) -> None:
    path = tmp_path / DEV_OVERRIDE_FILENAME
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(DevOverrideError):
        _read_override_api_base(str(path))


def test_read_non_object_or_non_string_raises(tmp_path) -> None:
    for content in ('[1, 2, 3]', '{"pairing_api_base": 123}'):
        path = tmp_path / DEV_OVERRIDE_FILENAME
        path.write_text(content, encoding="utf-8")
        with pytest.raises(DevOverrideError):
            _read_override_api_base(str(path))


@pytest.mark.asyncio
async def test_resolve_missing_returns_none(tmp_path) -> None:
    assert await resolve_dev_pairing_api_base(_fake_hass(tmp_path)) is None


@pytest.mark.asyncio
async def test_resolve_valid_returns_normalized_url(tmp_path) -> None:
    (tmp_path / DEV_OVERRIDE_FILENAME).write_text(
        '{"pairing_api_base": "http://192.168.9.99:5078/api/"}', encoding="utf-8"
    )
    # 归一化：去掉尾部斜杠。
    assert await resolve_dev_pairing_api_base(_fake_hass(tmp_path)) == "http://192.168.9.99:5078/api"


@pytest.mark.asyncio
async def test_resolve_invalid_scheme_raises(tmp_path) -> None:
    # 文件存在且地址非法（非 http(s)）→ fail-loud，拦住配对而非静默连生产。
    (tmp_path / DEV_OVERRIDE_FILENAME).write_text(
        '{"pairing_api_base": "ftp://evil.example.com"}', encoding="utf-8"
    )
    with pytest.raises(DevOverrideError):
        await resolve_dev_pairing_api_base(_fake_hass(tmp_path))


@pytest.mark.asyncio
async def test_resolve_no_scheme_raises(tmp_path) -> None:
    (tmp_path / DEV_OVERRIDE_FILENAME).write_text(
        '{"pairing_api_base": "192.168.9.99:5078"}', encoding="utf-8"
    )
    with pytest.raises(DevOverrideError):
        await resolve_dev_pairing_api_base(_fake_hass(tmp_path))
