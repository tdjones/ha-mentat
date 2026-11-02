"""A target as the readings the model judges, and what never leaves the house."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mentat.const import MAX_TARGET_ENTITIES
from custom_components.mentat.readings import async_records, async_state, attribute_subset

KETTLE = "sensor.kettle_temperature"
GATE = "binary_sensor.back_gate"
STUDY = "climate.study"


def _attributes_of(hass: HomeAssistant, entity_id: str, attributes: dict[str, Any]) -> Any:
    hass.states.async_set(entity_id, "on", attributes)
    [record] = async_records(hass, {"entity_id": [entity_id]}, with_attributes=True)
    return record["attributes"]


async def test_state_of_a_note_without_a_target_is_the_note(hass: HomeAssistant) -> None:
    assert async_state(hass, "the kettle boiled twice", None) == "the kettle boiled twice"
    assert async_state(hass, "the kettle boiled twice", {}) == "the kettle boiled twice"
    assert async_state(hass, None, {}) is None


async def test_state_with_a_target_holds_now_entities_and_the_note(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-04 18:05:00+00:00")
    hass.states.async_set(KETTLE, "97")
    state = async_state(hass, "Tea is at seven", {"entity_id": [KETTLE]})
    assert state == {
        "now": "2026-11-04 10:05 Wednesday",
        "entities": [{"entity_id": KETTLE, "state": "97", "changed": "0 s ago"}],
        "note": "Tea is at seven",
    }


async def test_state_with_a_target_and_no_note_has_no_note_key(hass: HomeAssistant) -> None:
    hass.states.async_set(KETTLE, "97")
    assert set(async_state(hass, None, {"entity_id": [KETTLE]})) == {"now", "entities"}


async def test_records_are_sorted_by_entity_id(hass: HomeAssistant) -> None:
    for entity_id in (STUDY, KETTLE, GATE):
        hass.states.async_set(entity_id, "1")
    records = async_records(hass, {"entity_id": [STUDY, KETTLE, GATE]})
    assert [record["entity_id"] for record in records] == [GATE, STUDY, KETTLE]


async def test_records_say_how_long_ago_the_state_changed(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    hass.states.async_set(GATE, "off")
    freezer.tick(timedelta(minutes=7))
    [record] = async_records(hass, {"entity_id": [GATE]})
    assert record["changed"] == "7 min ago"


async def test_records_skip_a_name_equal_to_the_entity_id(hass: HomeAssistant) -> None:
    hass.states.async_set(GATE, "off", {"friendly_name": GATE})
    hass.states.async_set(STUDY, "heat")
    gate, study = async_records(hass, {"entity_id": [GATE, STUDY]})
    assert "name" not in gate
    assert "name" not in study


async def test_records_keep_a_name_that_adds_information(hass: HomeAssistant) -> None:
    hass.states.async_set(GATE, "off", {"friendly_name": "Gate to the alley"})
    [record] = async_records(hass, {"entity_id": [GATE]})
    assert record["name"] == "Gate to the alley"


async def test_records_always_carry_device_class_and_unit(hass: HomeAssistant) -> None:
    hass.states.async_set(
        KETTLE,
        "97",
        {
            "device_class": "temperature",
            "unit_of_measurement": "°C",
            "icon": "mdi:kettle",
            "state_class": "measurement",
        },
    )
    [record] = async_records(hass, {"entity_id": [KETTLE]})
    assert record["device_class"] == "temperature"
    assert record["unit_of_measurement"] == "°C"
    assert "attributes" not in record


async def test_records_send_asked_for_attributes_even_when_none_are_left(
    hass: HomeAssistant,
) -> None:
    assert _attributes_of(hass, "lock.side_door", {"access_token": "abc123"}) == {}


async def test_attribute_subset_leaves_out_what_the_record_carries(hass: HomeAssistant) -> None:
    subset = attribute_subset(
        {
            "friendly_name": "Study",
            "device_class": "temperature",
            "unit_of_measurement": "°C",
            "hvac_action": "heating",
            "longitude": 5.1,
        }
    )
    assert subset == {"hvac_action": "heating"}


async def test_records_never_send_location_tokens_or_pictures(hass: HomeAssistant) -> None:
    attributes = _attributes_of(
        hass,
        "device_tracker.bike",
        {
            "battery_level": 64,
            "latitude": 51.4,
            "longitude": -0.1,
            "gps_accuracy": 12,
            "entity_picture": "/api/image/bike",
            "entity_picture_local": "/local/bike.png",
            "access_token": "zzz",
        },
    )
    assert attributes == {"battery_level": 64}


async def test_records_drop_keys_named_like_secrets_in_any_case(hass: HomeAssistant) -> None:
    attributes = _attributes_of(
        hass,
        "vacuum.downstairs",
        {
            "fan_speed": "quiet",
            "Session_Token": "t",
            "router_password": "p",
            "OAuth_Secret": "s",
            "thumbnail_picture": "/p.jpg",
            "cover_image_url_small": "/c.jpg",
            "image_urls": ["/1", "/2"],
            "Credentials": "c",
            "x_API_KEY_hash": "h",
        },
    )
    assert attributes == {"fan_speed": "quiet"}


async def test_records_drop_addresses_but_keep_words_that_contain_the_letters(
    hass: HomeAssistant,
) -> None:
    attributes = _attributes_of(
        hass,
        "camera.yard",
        {
            "URL": "http://192.0.2.20/live",
            "URI": "rtsp://192.0.2.20",
            "Feed_Url": "/feed",
            "webhook_uri": "/hook",
            "hurled": "a word, not an address",
        },
    )
    assert attributes == {"hurled": "a word, not an address"}


async def test_records_redact_nested_mappings_and_lists(hass: HomeAssistant) -> None:
    attributes = _attributes_of(
        hass,
        "camera.yard",
        {
            "feeds": [
                {"label": "yard", "snapshot_url": "/s.jpg", "fps": 10},
                ("spare", {"password": "p", "zoom": 2}),
            ],
            "zones": {"gate": {"api_key": "k", "armed": False}},
        },
    )
    assert attributes == {
        "feeds": [{"label": "yard", "fps": 10}, ["spare", {"zoom": 2}]],
        "zones": {"gate": {"armed": False}},
    }


async def test_records_take_the_area_from_the_entity_then_its_device(
    hass: HomeAssistant,
) -> None:
    config_entry = MockConfigEntry(domain="test")
    config_entry.add_to_hass(hass)
    areas = ar.async_get(hass)
    study = areas.async_create("Study")
    garage = areas.async_create("Garage")
    devices = dr.async_get(hass)
    charger = devices.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("test", "charger")}
    )
    devices.async_update_device(charger.id, area_id=garage.id)
    entities = er.async_get(hass)
    lamp = entities.async_get_or_create("light", "test", "lamp", suggested_object_id="lamp")
    entities.async_update_entity(lamp.entity_id, area_id=study.id)
    plug = entities.async_get_or_create(
        "switch", "test", "plug", device_id=charger.id, suggested_object_id="plug"
    )
    # The entity's own area wins over its device's.
    moved = entities.async_get_or_create(
        "switch", "test", "moved", device_id=charger.id, suggested_object_id="moved"
    )
    entities.async_update_entity(moved.entity_id, area_id=study.id)
    loose = entities.async_get_or_create("sensor", "test", "loose", suggested_object_id="loose")
    ids = [lamp.entity_id, plug.entity_id, moved.entity_id, loose.entity_id, "sensor.unlisted"]
    for entity_id in ids:
        hass.states.async_set(entity_id, "on")

    records = async_records(hass, {"entity_id": ids})
    assert {record["entity_id"]: record.get("area") for record in records} == {
        "light.lamp": "Study",
        "switch.plug": "Garage",
        "switch.moved": "Study",
        "sensor.loose": None,
        "sensor.unlisted": None,
    }
    # Absent rather than null: an empty field is billed like a full one.
    assert "area" not in next(r for r in records if r["entity_id"] == "sensor.loose")


async def test_records_refuse_a_target_that_does_not_exist(hass: HomeAssistant) -> None:
    hass.states.async_set(KETTLE, "97")
    with pytest.raises(ServiceValidationError) as err:
        async_records(hass, {"entity_id": [KETTLE], "area_id": ["attic"], "device_id": ["d0d0"]})
    assert err.value.translation_key == "target_missing"
    named = err.value.translation_placeholders["targets"]
    assert "attic" in named
    assert "d0d0" in named


async def test_records_refuse_a_target_without_a_state(hass: HomeAssistant) -> None:
    with pytest.raises(ServiceValidationError) as err:
        async_records(hass, {"entity_id": ["sensor.long_gone"]})
    assert err.value.translation_key == "empty_target"


async def test_records_refuse_more_entities_than_the_limit(hass: HomeAssistant) -> None:
    for entity_id in (KETTLE, GATE, STUDY):
        hass.states.async_set(entity_id, "1")
    with pytest.raises(ServiceValidationError) as err:
        async_records(hass, {"entity_id": [KETTLE, GATE, STUDY]}, limit=2)
    assert err.value.translation_key == "target_too_large"
    assert err.value.translation_placeholders == {"count": "3", "limit": "2"}


async def test_state_refuses_a_target_over_the_entity_limit(hass: HomeAssistant) -> None:
    ids = [f"sensor.probe_{n:03}" for n in range(MAX_TARGET_ENTITIES + 1)]
    for entity_id in ids:
        hass.states.async_set(entity_id, "0")
    with pytest.raises(ServiceValidationError) as err:
        async_state(hass, None, {"entity_id": ids})
    assert err.value.translation_placeholders == {
        "count": str(MAX_TARGET_ENTITIES + 1),
        "limit": str(MAX_TARGET_ENTITIES),
    }
