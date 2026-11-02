"""Picked entities as the readings the model judges.

A target (entities, devices, areas, floors, labels) becomes a list of small
records sorted by entity id. A record holds the id, the state, the friendly name
when it says more than the id, the unit, the device class, the area and how long
ago the state changed. The model reads field names as labels, so a record beats
a sentence somebody has to template by hand. The state sent is the note alone,
or `{now, entities, note?}` once entities are involved.

A target that names something that no longer exists is refused, because asking
about what is left answers a different question. So is a target with no entity
that has a state, and one with more entities than the limit, since every record
is billed on every request. Private keys (credentials, pictures, addresses,
locations) never leave, at any depth, whatever asked for attributes. Nothing
here sends a request or reads the recorder; window.py adds the log.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from operator import attrgetter
from typing import Any, Final

import orjson
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_FRIENDLY_NAME, ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.json import json_encoder_default
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids
from homeassistant.util import dt as dt_util
from homeassistant.util.json import json_loads

from .const import DOMAIN, MAX_TARGET_ENTITIES

# Sent for every entity that has them. Everything else waits for
# with_attributes: a forecast or a media player's artwork list is thousands of
# billed tokens of noise.
_ALWAYS: Final = (ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT)
# Never sent, even when every attribute is asked for. A camera's access token is
# a live credential, a picture URL often carries one, and a location is where a
# person is. None of them helps judge a state.
_NEVER: Final = frozenset(
    {
        "access_token",
        "entity_picture",
        "entity_picture_local",
        "latitude",
        "longitude",
        "gps_accuracy",
    }
)
# Nor anything whose name says it is one of those, whatever integration wrote it:
# a lock's `api_key`, a camera's `token_url`. `image_url` catches the names the
# address suffixes below miss, such as `image_urls` and `image_url_large`.
_NEVER_PARTS: Final = (
    "token",
    "password",
    "secret",
    "picture",
    "image_url",
    "credential",
    "api_key",
)
# And no address of any kind: a camera stream, a webhook or a signed download
# link is often a credential in all but name, and the model cannot open one.
_ADDRESS_KEYS: Final = frozenset({"url", "uri"})
_ADDRESS_SUFFIXES: Final = ("_url", "_uri")


@callback
def is_mentat_entity(hass: HomeAssistant, entity_id: str) -> bool:
    """Whether an entity is one of Mentat's own, from any entry.

    A watch woken by its own answer would ask again after every answer, and
    two entries reading each other's answers would wake each other, so these
    never trigger an evaluation and never enter a window's log. They may still
    be read as part of the state when a target picks them.
    """
    entry = er.async_get(hass).async_get(entity_id)
    return entry is not None and entry.platform == DOMAIN


def private(key: Any) -> bool:
    """Whether an attribute or event data key is never sent."""
    lowered = str(key).lower()
    return (
        lowered in _NEVER
        or lowered in _ADDRESS_KEYS
        or lowered.endswith(_ADDRESS_SUFFIXES)
        or any(part in lowered for part in _NEVER_PARTS)
    )


def redact(value: Any) -> Any:
    """The value with every private key dropped, at any depth.

    Event data and attributes nest: a doorbell event can carry
    `{"snapshot": {"url": ...}}` or a list of such records, and a check of the
    top level alone would send them.
    """
    if isinstance(value, Mapping):
        return {key: redact(item) for key, item in value.items() if not private(key)}
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


def _plain(value: Any) -> Any:
    """Home Assistant's own conversions, and the text of anything else."""
    try:
        return json_encoder_default(value)
    except TypeError:
        return str(value)


def json_safe(value: Any) -> Any:
    """The value as the plain data it reads as in JSON.

    Bus event data can hold anything: a state_changed event carries State
    objects, others datetimes or sets. Home Assistant's encoder knows those, and
    anything it does not know goes as its text, so what is kept is what the
    request can carry and what DecisionClient.body_size() measures.
    """
    return json_loads(orjson.dumps(value, option=orjson.OPT_NON_STR_KEYS, default=_plain))


# A record has its own fields for these, so the attributes leave them out.
_CARRIED: Final = frozenset({ATTR_FRIENDLY_NAME, *_ALWAYS})


def attribute_subset(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """An entity's attributes as with_attributes sends them: redacted, without
    the name, unit and device class a record already carries.
    """
    return {
        key: redact(value)
        for key, value in attributes.items()
        if key not in _CARRIED and not private(key)
    }


@dataclass(frozen=True, slots=True)
class _Registries:
    """The three registries an area lookup walks, fetched once per target."""

    entities: er.EntityRegistry
    devices: dr.DeviceRegistry
    areas: ar.AreaRegistry

    @classmethod
    def of(cls, hass: HomeAssistant) -> _Registries:
        return cls(er.async_get(hass), dr.async_get(hass), ar.async_get(hass))

    def area_of(self, entity_id: str) -> str | None:
        """The entity's own area, else its device's."""
        entry = self.entities.async_get(entity_id)
        if entry is None:
            return None
        device = self.devices.async_get(entry.device_id) if entry.device_id else None
        for area_id in (entry.area_id, device.area_id if device else None):
            if area_id is not None:
                area = self.areas.async_get_area(area_id)
                return area.name if area else None
        return None


@dataclass(frozen=True, slots=True)
class Reading:
    """One entity as the model reads it. Empty fields are left out of the record,
    because every key is billed on every request.
    """

    entity_id: str
    state: str
    changed: timedelta
    name: str | None = None
    unit: str | None = None
    device_class: str | None = None
    area: str | None = None
    # None when attributes were not asked for; {} when nothing was left to send.
    attributes: dict[str, Any] | None = None

    @classmethod
    def of(
        cls, state: State, registries: _Registries, now: datetime, with_attributes: bool
    ) -> Reading:
        given = state.attributes
        name = given.get(ATTR_FRIENDLY_NAME)
        return cls(
            entity_id=state.entity_id,
            state=state.state,
            changed=now - state.last_changed,
            # A name equal to the id tells the model nothing and is billed twice.
            name=name if name and name != state.entity_id else None,
            unit=given.get(ATTR_UNIT_OF_MEASUREMENT),
            device_class=given.get(ATTR_DEVICE_CLASS),
            area=registries.area_of(state.entity_id),
            attributes=attribute_subset(given) if with_attributes else None,
        )

    def as_record(self) -> dict[str, Any]:
        record = {
            "entity_id": self.entity_id,
            "name": self.name,
            "state": self.state,
            ATTR_UNIT_OF_MEASUREMENT: self.unit,
            ATTR_DEVICE_CLASS: self.device_class,
            "area": self.area,
            # Phrased like a window's `since`, so both kinds of state read alike.
            "changed": ago(self.changed),
            "attributes": self.attributes,
        }
        return {key: value for key, value in record.items() if value is not None}


def _refuse(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
    )


@callback
def async_records(
    hass: HomeAssistant,
    target: Mapping[str, Any],
    *,
    with_attributes: bool = False,
    limit: int = MAX_TARGET_ENTITIES,
) -> list[dict[str, Any]]:
    """One record per entity the target holds that has a state, by entity id.

    Raises ServiceValidationError for a target naming a floor, area, device or
    label that does not exist, for one that holds no entity with a state, and
    for one holding more than `limit` entities.
    """
    picked = async_extract_referenced_entity_ids(hass, TargetSelection(dict(target)))
    gone = (
        picked.missing_floors
        | picked.missing_areas
        | picked.missing_devices
        | picked.missing_labels
    )
    if gone:
        raise _refuse("target_missing", targets=", ".join(sorted(gone)))
    states = sorted(
        filter(None, map(hass.states.get, picked.referenced | picked.indirectly_referenced)),
        key=attrgetter("entity_id"),
    )
    if not states:
        raise _refuse("empty_target")
    if len(states) > limit:
        raise _refuse("target_too_large", count=str(len(states)), limit=str(limit))
    registries = _Registries.of(hass)
    now = dt_util.utcnow()
    return [Reading.of(state, registries, now, with_attributes).as_record() for state in states]


@callback
def async_state(
    hass: HomeAssistant,
    note: Any | None,
    target: Mapping[str, Any] | None,
    *,
    with_attributes: bool = False,
) -> Any:
    """The note alone, or the target's records with the local time and the note."""
    if not target:
        return note
    state: dict[str, Any] = {
        "now": dt_util.now().strftime("%Y-%m-%d %H:%M %A"),
        "entities": async_records(hass, target, with_attributes=with_attributes),
    }
    if note:
        state["note"] = note
    return state


def span(delta: timedelta) -> str:
    """A length of time the way a person says it, so the model does no arithmetic."""
    seconds = max(0, round(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{round(seconds / 60)} min"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


def ago(delta: timedelta) -> str:
    return f"{span(delta)} ago"
