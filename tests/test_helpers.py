from tests.helpers import AsyncFakeMQTTClient


async def test_fake_mqtt_client_records_publish_calls() -> None:
    client = AsyncFakeMQTTClient()

    await client.publish("demo/topic", "{}", qos=1, retain=True)

    assert client.published[0]["topic"] == "demo/topic"
    assert client.published[0]["retain"] is True
