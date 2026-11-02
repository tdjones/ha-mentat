"""Measure what a decision model bills, so const.py and providers.py can cite it.

Run from the repository root with the project's Python. The module form puts the
repository on the import path:

    .venv/bin/python -m scripts.measure --url http://127.0.0.1:8000 \\
        --dialect clef --model clef-flash --no-truncate
    MENTAT_API_KEY=... .venv/bin/python -m scripts.measure \\
        --url https://api.typesafe.ai --dialect typesafe --model jev-latest

One run sends up to thirteen requests through the client Home Assistant builds:
the setup probe, a state of 0, 5, 10 and 20 entity records twice each, and one
280 x 280 and one 1024 x 768 picture, each followed by the same body without its
picture. Billed input tokens of the sized states are fitted against body bytes as
tokens = fixed part + bytes / ratio, which is the line the ledger estimates with.
The script prints markdown rows for docs/measurements.md and a receipt per call.
It never prints the key.

The tests never import this file, and it needs no running Home Assistant.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from math import ceil
from typing import Any

import aiohttp
from homeassistant.helpers.json import json_dumps
from jevclient import JevError, Noul, Question
from PIL import Image as PILImage

from custom_components.mentat.client import DecisionClient, StateType, build_client
from custom_components.mentat.const import (
    BUDGET_ESTIMATE_MARGIN,
    COLD_START_BYTES_PER_TOKEN,
    CONF_DIALECT,
    CONF_IMAGE_FORMAT,
    CONF_PROVIDER,
    PROBE_INSTRUCTIONS,
    PROBE_KEY,
    PROBE_STATE,
    REQUEST_OVERHEAD_TOKENS,
)
from custom_components.mentat.pictures import Pictures, attach, prepare_image
from custom_components.mentat.providers import (
    PROVIDER_GENERIC,
    Dialect,
    ImageFormat,
    image_limits,
    image_tokens,
    provider_for,
)

SIZES = (0, 5, 10, 20)
REPEATS = 2
PICTURE_SIZES = ((280, 280), (1024, 768))
NOW = "2026-11-01 12:00 Sunday"
QUESTION_KEY = "someone_home"
QUESTION = Noul(
    "Is someone likely to be at home right now?",
    true="Someone is home",
    false="The house is empty",
)
PICTURE_QUESTION = Noul("Does the picture show a door?")

# Twenty generic devices, cycled to build a target. The ids follow the ones the
# README uses, so nothing here describes a real house.
_DEVICES: tuple[tuple[str, str, str, str | None, str | None, str], ...] = (
    ("sensor.washing_machine_power", "Washing machine power", "3.2", "W", "power", "Utility"),
    ("binary_sensor.front_door", "Front door", "off", None, "door", "Hall"),
    ("light.hallway", "Hallway", "on", None, None, "Hall"),
    ("sensor.lounge_temperature", "Lounge temperature", "21.4", "°C", "temperature", "Lounge"),
    ("binary_sensor.kitchen_motion", "Kitchen motion", "on", None, "motion", "Kitchen"),
    ("media_player.living_room_tv", "Living room TV", "playing", None, None, "Living room"),
    ("sensor.dishwasher_power", "Dishwasher power", "0.4", "W", "power", "Kitchen"),
    ("lock.back_door", "Back door", "locked", None, None, "Kitchen"),
    ("sensor.bedroom_humidity", "Bedroom humidity", "48", "%", "humidity", "Bedroom"),
    ("climate.thermostat", "Thermostat", "heat", None, None, "Hall"),
    ("binary_sensor.garage_door", "Garage door", "off", None, "garage_door", "Garage"),
    ("sensor.outdoor_temperature", "Outdoor temperature", "8.9", "°C", "temperature", "Garden"),
    ("switch.coffee_maker", "Coffee maker", "off", None, None, "Kitchen"),
    ("cover.living_room_blinds", "Living room blinds", "open", None, None, "Living room"),
    ("sensor.grid_power", "Grid power", "412", "W", "power", "Utility"),
    ("binary_sensor.bedroom_window", "Bedroom window", "off", None, "window", "Bedroom"),
    ("light.porch", "Porch", "off", None, None, "Garden"),
    ("sensor.office_co2", "Office CO2", "640", "ppm", "carbon_dioxide", "Office"),
    ("device_tracker.phone_one", "Phone one", "home", None, None, "Hall"),
    ("vacuum.robot", "Robot vacuum", "docked", None, None, "Hall"),
)


def records(count: int) -> list[dict[str, Any]]:
    """`count` records shaped like readings.async_records output, by entity id."""
    made = []
    for index in range(count):
        entity_id, name, state, unit, device_class, area = _DEVICES[index % len(_DEVICES)]
        record: dict[str, Any] = {"entity_id": entity_id, "name": name, "state": state}
        if unit is not None:
            record["unit_of_measurement"] = unit
        if device_class is not None:
            record["device_class"] = device_class
        record["area"] = area
        record["changed"] = f"{3 + 7 * index} min ago"
        made.append(record)
    return sorted(made, key=lambda record: str(record["entity_id"]))


def synthetic_state(count: int) -> dict[str, Any]:
    """The state readings.async_state builds for a target of `count` entities."""
    return {"now": NOW, "entities": records(count)}


def jpeg(width: int, height: int) -> bytes:
    """A gradient JPEG. Billing goes by pixels, so the content does not matter."""
    picture = PILImage.linear_gradient("L").resize((width, height)).convert("RGB")
    buffer = io.BytesIO()
    picture.save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


@dataclass(frozen=True, slots=True)
class Receipt:
    label: str
    body_bytes: int
    input_tokens: int
    output_tokens: int
    latency_ms: float
    model: str


async def send(
    client: DecisionClient,
    label: str,
    state: StateType,
    questions: Mapping[str, Question],
    pictures: Pictures | None = None,
) -> Receipt:
    body_bytes = client.body_size(state, questions)
    reply = await client.ask(state, questions, pictures=pictures)
    return Receipt(
        label=label,
        body_bytes=body_bytes,
        input_tokens=reply.usage.input_tokens,
        output_tokens=reply.usage.output_tokens,
        latency_ms=reply.latency_ms,
        model=reply.model,
    )


def fit(points: list[tuple[int, int]]) -> tuple[float, float]:
    """Least squares over (bytes, tokens): the fixed part and bytes per token."""
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    spread = sum((x - mean_x) ** 2 for x, _ in points)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / spread
    return mean_y - slope * mean_x, 1 / slope


async def measure(args: argparse.Namespace) -> int:
    dialect = Dialect(args.dialect)
    provider = provider_for(
        {
            CONF_PROVIDER: PROVIDER_GENERIC,
            CONF_DIALECT: dialect.value,
            CONF_IMAGE_FORMAT: args.image_format,
        }
    )
    extra = None if args.truncate is None else {"truncate": args.truncate}
    # Home Assistant's own session encodes with json_dumps, so jevclient's json=
    # posts the same bytes here as it does inside Home Assistant.
    async with aiohttp.ClientSession(json_serialize=json_dumps) as session:
        client = build_client(
            provider,
            api_key=args.key or "",
            base_url=args.url,
            model=args.model,
            account_id=None,
            image_format=provider.image_format,
            extra=extra,
            session=session,
        )
        receipts = [await send(client, "probe", PROBE_STATE, {PROBE_KEY: Noul(PROBE_INSTRUCTIONS)})]
        for count in SIZES:
            for repeat in range(1, REPEATS + 1):
                receipts.append(
                    await send(
                        client,
                        f"{count} records, run {repeat}",
                        synthetic_state(count),
                        {QUESTION_KEY: QUESTION},
                    )
                )
        sized = receipts[1:]
        overhead, ratio = fit([(r.body_bytes, r.input_tokens) for r in sized])

        picture_rows = []
        limits = image_limits(dialect)
        for width, height in PICTURE_SIZES:
            source = f"gradient {width} x {height}"
            picture = prepare_image(jpeg(width, height), source, limits)
            pictures = Pictures.empty(dialect)
            pictures.add({"source": source}, picture)
            state = attach("One picture from a test pattern.", pictures)
            questions = {"door": PICTURE_QUESTION}
            receipt = await send(client, source, state, questions, pictures)
            # The same body without the picture, so the difference is the picture
            # alone. A server that refuses a state naming a picture it was not sent
            # falls back to the fitted line, which misses by up to the fit's error.
            try:
                bare = await send(client, f"{source}, text only", state, questions)
            except JevError:
                text_part = overhead + receipt.body_bytes / ratio
                how = "text part from the fit"
            else:
                receipts.append(bare)
                text_part = bare.input_tokens
                how = "text part sent alone"
            receipts.append(receipt)
            billed = round(receipt.input_tokens - text_part)
            formula = image_tokens(dialect, *picture.sent)
            picture_rows.append((picture.sent, billed, formula, how))
        await client.async_close()

    model = receipts[0].model
    today = date.today().isoformat()
    residual = max(abs(r.input_tokens - (overhead + r.body_bytes / ratio)) for r in sized)
    largest = max(sized, key=lambda r: r.body_bytes)
    by_body: dict[int, list[int]] = {}
    for r in sized:
        by_body.setdefault(r.body_bytes, []).append(r.input_tokens)
    repeat_spread = max((max(t) - min(t)) / min(t) for t in by_body.values())
    per_record = (
        sum(r.input_tokens for r in sized if r.label.startswith(f"{SIZES[-1]} "))
        - sum(r.input_tokens for r in sized if r.label.startswith(f"{SIZES[0]} "))
    ) / (REPEATS * (SIZES[-1] - SIZES[0]))
    cold = ceil(
        (REQUEST_OVERHEAD_TOKENS + largest.body_bytes / COLD_START_BYTES_PER_TOKEN)
        * BUDGET_ESTIMATE_MARGIN
    )

    rows = [
        ("Setup probe, billed", f"{receipts[0].input_tokens} tokens"),
        ("Fixed part per request, fitted", f"{overhead:.0f} tokens"),
        ("Body bytes per billed token, fitted", f"{ratio:.2f}"),
        ("Largest miss of the fit", f"{residual:.1f} tokens"),
        ("Spread between two sends of one body", f"{repeat_spread:.1%}"),
        ("Tokens per entity record", f"{per_record:.1f}"),
        (
            f"Cold-start estimate for the {SIZES[-1]} record body",
            f"{cold} tokens against {largest.input_tokens} billed",
        ),
    ]
    for (width, height), billed, formula, how in picture_rows:
        rows.append(
            (
                f"Picture sent at {width} x {height}",
                f"{billed} tokens, formula says {formula} ({how})",
            )
        )

    print("| Figure | Value | Model | Date |")
    print("| --- | --- | --- | --- |")
    for figure, value in rows:
        print(f"| {figure} | {value} | {model} | {today} |")
    print()
    print("| Call | Body bytes | Input tokens | Output tokens | Latency ms |")
    print("| --- | --- | --- | --- | --- |")
    for r in receipts:
        print(
            f"| {r.label} | {r.body_bytes} | {r.input_tokens} | {r.output_tokens} "
            f"| {r.latency_ms:.0f} |"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="the server's base address")
    parser.add_argument("--dialect", required=True, choices=[d.value for d in Dialect])
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--key",
        default=os.environ.get("MENTAT_API_KEY"),
        help="the API key; MENTAT_API_KEY in the environment keeps it out of shell history",
    )
    parser.add_argument("--image-format", choices=[f.value for f in ImageFormat], default=None)
    parser.add_argument(
        "--truncate",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="send a truncate flag, which clef_mlx.py reads; left out by default",
    )
    try:
        return asyncio.run(measure(parser.parse_args()))
    except JevError as err:
        print(f"The server refused a measurement request: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
