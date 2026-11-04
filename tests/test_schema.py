"""The YAML schema refuses, by the field the user wrote, what the API would refuse."""

from __future__ import annotations

from typing import Any

import pytest
import voluptuous as vol

from custom_components.mentat.const import DOMAIN
from custom_components.mentat.schema import (
    CONFIG_SCHEMA,
    EVENT_FILTER_SCHEMA,
    QUESTION_SCHEMA,
    TARGET_SCHEMA,
    WATCH_SCHEMA,
    WINDOW_SCHEMA,
    WORDING,
)

KETTLE = "switch.kettle"


def kitchen(**changes: Any) -> dict[str, Any]:
    """A valid watch, with fields replaced; a None drops the field."""
    watch: dict[str, Any] = {
        "name": "Kitchen",
        "entities": [KETTLE],
        "scan_interval": 900,
        "questions": [{"name": "Boiled", "type": "noul", "instructions": "Has it boiled?"}],
        **changes,
    }
    return {key: value for key, value in watch.items() if value is not None}


def asks(*names: str) -> list[dict[str, Any]]:
    return [{"name": name, "type": "noul", "instructions": "x"} for name in names]


# --- questions ---


@pytest.mark.parametrize(
    ("fields", "fragment"),
    [
        ({"type": "noul", "criteria": {"hot": None}}, "a noul takes 'high:' and 'low:'"),
        ({"type": "choice", "criteria": {"hot": None}}, "mapping of 2 to 255 options"),
        ({"type": "choice", "criteria": ["hot", "cold"]}, "mapping of 2 to 255 options"),
        ({"type": "choice"}, "mapping of 2 to 255 options"),
        ({"type": "score", "criteria": ["lukewarm"]}, "list of 2 to 10 levels"),
        ({"type": "score", "criteria": [str(n) for n in range(11)]}, "list of 2 to 10 levels"),
        ({"type": "score", "criteria": {"a": None, "b": None}}, "list of 2 to 10 levels"),
        ({"type": "score", "criteria": ["cold", "hot"], "cutoff": 0.4}, "apply to a noul only"),
        ({"type": "choice", "criteria": {"a": None, "b": None}, "low": "n"}, "noul only"),
        ({"type": "noul", "low": False}, "put the word in quotes"),
        ({"type": "score", "criteria": [True, False]}, "put the word in quotes"),
        ({"type": "noul", "cutoff": 1.5}, "value must be at most 1"),
        ({"type": "noul", "release": 0.3}, "so it needs 'cutoff:'"),
        (
            {"type": "noul", "cutoff": 0.6, "release": 0.7},
            r"\(0.7\) cannot be over 'cutoff:' \(0.6\)",
        ),
        ({"type": "score", "criteria": ["cold", "hot"], "release": 0.4}, "apply to a noul only"),
        ({"type": "box"}, "value must be one of"),
    ],
)
def test_question_schema_refuses_a_shape_the_api_refuses(
    fields: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(vol.Invalid, match=fragment):
        QUESTION_SCHEMA({"name": "Kettle", "instructions": "x", **fields})


def test_question_schema_names_the_question() -> None:
    with pytest.raises(vol.Invalid, match="question 'Kettle': a noul takes"):
        QUESTION_SCHEMA(
            {"name": "Kettle", "type": "noul", "instructions": "x", "criteria": {"a": None}}
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"type": "noul", "high": "Steam", "low": "Quiet", "cutoff": "0.25"},
        {"type": "noul", "cutoff": 0.7, "release": 0.3},
        {"type": "noul", "cutoff": 0.7, "release": 0.7},
        {"type": "choice", "criteria": {"boiling": "Steam", "off": None}},
        {"type": "score", "criteria": ["cold", {"warm": "hand hot"}, "boiling"]},
    ],
)
def test_question_schema_accepts_each_type(fields: dict[str, Any]) -> None:
    assert QUESTION_SCHEMA({"name": "Kettle", "instructions": "x", **fields})["name"] == "Kettle"


def test_wording_accepts_text_and_structure() -> None:
    assert WORDING("on") == "on"
    assert WORDING({"steam": True}) == {"steam": True}
    with pytest.raises(vol.Invalid, match="put the word in quotes"):
        WORDING(True)


# --- windows ---


def test_window_schema_fills_the_defaults() -> None:
    window = WINDOW_SCHEMA({"duration": "00:10:00"})
    assert (window["max_events"], window["settle"], window["evaluate"]) == (100, 2.0, "both")
    assert window["events"] == []
    assert window["collapse_repeats"] is True
    assert window["with_attributes"] is False


@pytest.mark.parametrize(
    ("window", "fragment"),
    [
        ({"duration": 59}, "from 1 minute to 24 hours"),
        ({"duration": "24:00:01"}, "from 1 minute to 24 hours"),
        ({"duration": 90, "settle": 91}, "cannot be longer than the window"),
        ({"duration": 900, "max_events": 9}, "value must be at least 10"),
        ({"duration": 900, "max_events": 501}, "value must be at most 500"),
        ({"duration": 900, "settle": -0.5}, "value must be at least 0"),
        ({"duration": 900, "settle": 300.5}, "value must be at most 300"),
        ({"duration": 900, "evaluate": "hourly"}, "value must be one of"),
        (
            {"duration": 900, "events": [{"event_type": "mentat_decision"}]},
            "cannot log 'mentat_decision'",
        ),
        (
            {"duration": 900, "events": [{"event_type": "mentat_whatever"}]},
            "Mentat fires it after every answer",
        ),
    ],
)
def test_window_schema_refuses_out_of_range(window: dict[str, Any], fragment: str) -> None:
    with pytest.raises(vol.Invalid, match=fragment):
        WINDOW_SCHEMA(window)


@pytest.mark.parametrize("settle", [0, 300])
def test_window_schema_settle_spans_zero_to_five_minutes(settle: int) -> None:
    assert WINDOW_SCHEMA({"duration": 900, "settle": settle})["settle"] == settle


def test_event_filter_schema_defaults_its_data() -> None:
    assert EVENT_FILTER_SCHEMA({"event_type": "kettle_on"}) == {
        "event_type": "kettle_on",
        "data": {},
    }


# --- watches ---


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"entities": None}, "give it 'entities:' to look at, 'note:'"),
        ({"questions": asks("Boiled", "BOILED")}, "both become the key 'boiled'"),
        (
            {"entities": None, "note": "Guests", "window": {"duration": 900}},
            "a window logs changes to entities, so it needs 'entities:'",
        ),
        (
            {"scan_interval": None, "window": {"duration": 900, "evaluate": "interval"}},
            "so it needs 'scan_interval:'",
        ),
        ({"scan_interval": 0}, "value must be at least 1"),
        ({"questions": []}, "length of value must be at least 1"),
    ],
)
def test_watch_schema_refuses(changes: dict[str, Any], fragment: str) -> None:
    with pytest.raises(vol.Invalid, match=fragment):
        WATCH_SCHEMA(kitchen(**changes))


def test_watch_schema_names_the_watch() -> None:
    with pytest.raises(vol.Invalid, match="watch 'Kitchen': give it"):
        WATCH_SCHEMA(kitchen(entities=None))


def test_watch_schema_note_alone_is_enough() -> None:
    raw = WATCH_SCHEMA(kitchen(entities=None, note="The kettle is on"))
    assert raw["note"].template == "The kettle is on"


def test_watch_schema_a_one_second_schedule_is_fine() -> None:
    assert WATCH_SCHEMA(kitchen(scan_interval=1))["scan_interval"] == 1


def test_watch_schema_interval_window_with_a_schedule_is_fine() -> None:
    raw = WATCH_SCHEMA(kitchen(window={"duration": 900, "evaluate": "interval"}))
    assert raw["window"]["evaluate"] == "interval"


def test_watch_schema_leaves_an_unwritten_schedule_out() -> None:
    raw = WATCH_SCHEMA(kitchen(scan_interval=None))
    assert "scan_interval" not in raw
    assert (raw["wake_on"], raw["fire_event"], raw["with_attributes"]) == ([], True, False)


def test_watch_schema_entity_list_becomes_a_target() -> None:
    assert WATCH_SCHEMA(kitchen())["entities"] == {"entity_id": [KETTLE]}


def test_watch_schema_full_target_ids_become_lists() -> None:
    raw = WATCH_SCHEMA(kitchen(entities={"floor_id": "ground", "label_id": ["appliances"]}))
    assert raw["entities"] == {"floor_id": ["ground"], "label_id": ["appliances"]}


def test_watch_schema_refuses_a_bad_entity_id() -> None:
    with pytest.raises(vol.Invalid, match="invalid entity ID"):
        WATCH_SCHEMA(kitchen(entities=["kettle"]))


def test_target_schema_refuses_an_unknown_kind() -> None:
    with pytest.raises(vol.Invalid, match="not a valid option"):
        TARGET_SCHEMA({"room_id": ["kitchen"]})


# --- the whole block ---


def test_config_schema_refuses_two_watches_with_one_key() -> None:
    with pytest.raises(vol.Invalid, match="'Kitchen' and 'kitchen!' both become the key 'kitchen'"):
        CONFIG_SCHEMA({DOMAIN: [kitchen(), kitchen(name="kitchen!")]})


def test_config_schema_refuses_question_keys_that_meet_across_watches() -> None:
    first = kitchen(name="Back door", questions=asks("Open"))
    second = kitchen(name="Back", questions=asks("Door open"))
    with pytest.raises(vol.Invalid) as err:
        CONFIG_SCHEMA({DOMAIN: [first, second]})
    message = str(err.value)
    assert "question 'Open' in watch 'Back door'" in message
    assert "question 'Door open' in watch 'Back'" in message
    assert "'back_door_open'" in message


def test_config_schema_takes_one_watch_without_a_list() -> None:
    config = CONFIG_SCHEMA({DOMAIN: kitchen(), "other": 1})
    assert [watch["name"] for watch in config[DOMAIN]] == ["Kitchen"]
