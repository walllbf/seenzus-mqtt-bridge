# HA Behavior Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add comprehensive Home Assistant behavior tests for the bridge without changing runtime semantics.

**Architecture:** Introduce an isolated pytest-based test harness alongside the existing custom integration code. Cover bridge behavior through focused helper-level tests and coordinator/config-entry behavior tests using fake MQTT clients and lightweight Home Assistant stubs so production behavior remains unchanged.

**Tech Stack:** Python 3.11, pytest, pytest-asyncio, Home Assistant test dependencies, unittest.mock

---

### Task 1: Add test tooling entrypoints

**Files:**
- Create: `C:\Users\14845\Desktop\项目\Agent\Seenzus\seenzusaimqttbridge\requirements_test.txt`
- Create: `C:\Users\14845\Desktop\项目\Agent\Seenzus\seenzusaimqttbridge\pytest.ini`
- Create: `C:\Users\14845\Desktop\项目\Agent\Seenzus\seenzusaimqttbridge\tests\__init__.py`
- Modify: `C:\Users\14845\Desktop\项目\Agent\Seenzus\seenzusaimqttbridge\README.md`
- Modify: `C:\Users\14845\Desktop\项目\Agent\Seenzus\seenzusaimqttbridge\USER_MANUAL_zh-CN.md`

- [ ] **Step 1: Write the failing test/tooling expectation**

```python
def test_pytest_config_exists():
    from pathlib import Path

    assert Path("pytest.ini").exists()
    assert Path("requirements_test.txt").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tooling_smoke.py -q`
Expected: FAIL because `pytest.ini` and `requirements_test.txt` do not exist yet

- [ ] **Step 3: Write minimal implementation**

```ini
# pytest.ini
[pytest]
testpaths = tests
asyncio_mode = auto
```

```txt
# requirements_test.txt
pytest
pytest-asyncio
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tooling_smoke.py -q`
Expected: PASS

### Task 2: Add reusable HA/MQTT test helpers

**Files:**
- Create: `C:\Users\14845\Desktop\项目\Agent\Seenzus\seenzusaimqttbridge\tests\helpers.py`
- Create: `C:\Users\14845\Desktop\项目\Agent\Seenzus\seenzusaimqttbridge\tests\conftest.py`
- Test: `C:\Users\14845\Desktop\项目\Agent\Seenzus\seenzusaimqttbridge\tests\test_helpers.py`

- [ ] **Step 1: Write the failing test**

```python
from tests.helpers import FakeMQTTClient


def test_fake_mqtt_client_records_publish_calls():
    client = FakeMQTTClient()
    client.record_publish("demo/topic", "{}", qos=1, retain=True)

    assert client.published[0]["topic"] == "demo/topic"
    assert client.published[0]["retain"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_helpers.py -q`
Expected: FAIL because `tests.helpers` does not exist yet

- [ ] **Step 3: Write minimal implementation**

```python
class FakeMQTTClient:
    def __init__(self) -> None:
        self.published = []

    def record_publish(self, topic: str, payload: str, *, qos: int, retain: bool = False) -> None:
        self.published.append(
            {"topic": topic, "payload": payload, "qos": qos, "retain": retain}
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_helpers.py -q`
Expected: PASS

### Task 3: Cover bridge topic and retained cleanup behavior

**Files:**
- Modify: `C:\Users\14845\Desktop\项目\Agent\Seenzus\seenzusaimqttbridge\test_bridge_retained_topics.py`
- Test: `C:\Users\14845\Desktop\项目\Agent\Seenzus\seenzusaimqttbridge\tests\test_bridge_protocol_behavior.py`

- [ ] **Step 1: Write the failing test**

```python
from bridge_protocol import build_topics


def test_build_topics_uses_expected_presence_topic():
    topics = build_topics("savant/v2", "ha-demo")
    assert topics.presence_topic == "savant/v2/bridge/ha-demo/presence"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_bridge_protocol_behavior.py -q`
Expected: FAIL because the new pytest test file does not exist yet

- [ ] **Step 3: Write minimal implementation**

```python
def test_build_topics_uses_expected_presence_topic():
    topics = build_topics("savant/v2", "ha-demo")
    assert topics.presence_topic == "savant/v2/bridge/ha-demo/presence"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_bridge_protocol_behavior.py -q`
Expected: PASS

### Task 4: Cover coordinator state filtering and presence publishing

**Files:**
- Create: `C:\Users\14845\Desktop\项目\Agent\Seenzus\seenzusaimqttbridge\tests\test_coordinator_presence.py`
- Modify: `C:\Users\14845\Desktop\项目\Agent\Seenzus\seenzusaimqttbridge\__init__.py` (only if a tiny extraction is needed for testability)

- [ ] **Step 1: Write the failing test**

```python
async def test_publish_state_from_event_ignores_bridge_internal_entity(coordinator, fake_event, fake_client):
    coordinator._mqtt_client = fake_client
    await coordinator._publish_state_from_event(fake_event("sensor.savanai_bridge_status"))

    assert fake_client.published == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_coordinator_presence.py -q`
Expected: FAIL because coordinator fixtures/test helpers do not exist yet

- [ ] **Step 3: Write minimal implementation**

```python
@pytest.fixture
def fake_client():
    return AsyncFakeMQTTClient()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_coordinator_presence.py -q`
Expected: PASS for the internal-entity filtering case

### Task 5: Cover reload and retained cleanup behavior

**Files:**
- Create: `C:\Users\14845\Desktop\项目\Agent\Seenzus\seenzusaimqttbridge\tests\test_reload_behavior.py`
- Modify: `C:\Users\14845\Desktop\项目\Agent\Seenzus\seenzusaimqttbridge\__init__.py` (only if tiny extractions are needed)

- [ ] **Step 1: Write the failing test**

```python
async def test_prepare_for_reload_clears_previous_retained_presence_when_bridge_changes(coordinator, fake_client):
    coordinator._mqtt_client = fake_client
    coordinator._topics = build_topics("savant/v2", "ha-old")
    coordinator._resolve_topics = lambda: build_topics("savant/v2", "ha-new")

    await coordinator.async_prepare_for_reload()

    assert fake_client.published[0]["topic"] == "savant/v2/bridge/ha-old/presence"
    assert fake_client.published[0]["payload"] == ""
    assert fake_client.published[0]["retain"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_reload_behavior.py -q`
Expected: FAIL because the pytest test file/fixtures do not exist yet

- [ ] **Step 3: Write minimal implementation**

```python
async def test_prepare_for_reload_clears_previous_retained_presence_when_bridge_changes(...):
    ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_reload_behavior.py -q`
Expected: PASS

### Task 6: Final verification and documentation

**Files:**
- Modify: `C:\Users\14845\Desktop\项目\Agent\Seenzus\seenzusaimqttbridge\README.md`
- Modify: `C:\Users\14845\Desktop\项目\Agent\Seenzus\seenzusaimqttbridge\USER_MANUAL_zh-CN.md`

- [ ] **Step 1: Update documentation**

```markdown
- 补充测试环境准备方式
- 补充可运行的测试命令
- 说明测试覆盖的关键行为范围
```

- [ ] **Step 2: Run full verification**

Run: `python -m pytest tests test_bridge_retained_topics.py test_entity_filters.py -q`
Expected: PASS

- [ ] **Step 3: Run compile verification**

Run: `python -m compileall __init__.py config_flow.py bridge_protocol.py entity_filters.py ha_dispatcher.py pairing_client.py sensor.py`
Expected: PASS
