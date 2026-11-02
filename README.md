# Mentat

Mentat connects Home Assistant to a System One decision model such as Jev or Clef.
You write a question about your home in plain language. The model answers with a
typed value: a probability, one option out of a set, or a level on a scale you
define. It never writes prose, so an automation can act on the answer without
parsing a sentence.

The domain is `mentat`. Every answer costs a paid request on hosted providers, so
Mentat counts input tokens, estimates the cost, and refuses a request that would
go over your daily budget before it is sent.

## Question types

| Type | Answer | Entity |
| --- | --- | --- |
| `noul` | A probability from 0 to 1 that the statement is true. | A sensor with the probability. With a `cutoff`, also a verdict binary sensor that is on once the probability reaches it. |
| `choice` | One of 2 to 255 options, with a confidence and the probability of every option. | A sensor whose state is the winning option. |
| `score` | A weighted level on 2 to 10 levels you list lowest first, with the nearest level and a confidence. | A sensor with the weighted level and the levels as a legend. |

The actions also take a fourth type, `box`, which finds one thing in a picture and
returns its edges.

## Providers and dialects

| Provider | Dialect | What you enter |
| --- | --- | --- |
| TypeSafe Jev | TypeSafe | API key |
| OpenRouter | TypeSafe | API key |
| Cloudflare Workers AI, Clef or Clef-flash | Clef | API token with Workers AI access, and the 32 character account id |
| Local Clef server | Clef | The server's address; a key only if it asks for one |
| System One server | TypeSafe or Clef, your choice | The server's address, the dialect, and a key if it needs one |

A dialect is the wire format. TypeSafe is the published `/v1/systemone` API, where
pictures travel as named assets that the state refers to. Clef sends pictures as an
ordered list before the state and may wrap the reply in Cloudflare's
`{success, result, errors}` envelope.

A local Clef server is one you run yourself, for example a `/v1/systemone` server
on a Mac on your network at `http://192.0.2.20:8000`. Requests to it cost nothing,
so its price defaults to 0.

## Install

Mentat is not in the HACS default list. Install it by hand:

1. Copy `custom_components/mentat` from this repository into
   `/config/custom_components/mentat` on your Home Assistant host.
2. Restart Home Assistant.

From a clone, `make deploy HA_HOST=root@192.0.2.10` copies the folder over SSH.
It needs an SSH app on the host. `HA_CONFIG` changes the target folder from
`/config`.

Mentat needs Home Assistant 2026.9 or later.

## Setup

Go to Settings, Devices & services, Add integration, and pick Mentat. Choose a
provider and paste its key. Cloudflare asks for the account id on a second page.
For a local or System One server, open Advanced and give the address, the model,
and the dialect.

Setup sends one short probe question, so a wrong key, address or dialect fails
before the entry is saved. On clef-flash the probe billed 143 input tokens, which
at TypeSafe's price is under a thousandth of a cent.

The entry's options hold the money settings:

- **Input tokens allowed per day.** 0 turns the limit off. The count starts again
  at midnight in your Home Assistant time zone.
- **Dollars per million input tokens.** Only the spend sensor uses it.
- **Pictures per request.** At most 4.

A rejected key starts reauthentication. Reconfigure changes the provider, key or
address and keeps your questions.

## Questions in the UI

On the Mentat entry, choose Ask something new. Pick the answer type, write the
question, and pick the entities to read, by entity, device, area or label. A note
or template, a brief of standing facts, and a cutoff for a noul are optional.

Under Schedule and extras, a question asks every 5 minutes by default and a
second after a picked entity changes. The interval can be as short as one second;
the daily budget is what limits the spend. A watch never asks twice at once:
whatever wakes it while a request is out is answered by one more request after
it. Questions that read the same thing on the same schedule share one request.
A change to a Mentat entity, of any entry, never wakes a question or enters a
window's log, even when the target picks it; its state is still sent.

The last page, Trial run, shows the state the question sends and asks it once,
so you see a real answer and its cost before saving. The trial counts against the
budget like any other request.

## Questions in YAML

Each item under `mentat:` is a watch: one request per evaluation, with every
question in it judged against the same state. One Mentat entry runs every YAML
watch. At start it is the oldest enabled entry that loads. When that entry is
disabled, removed or fails to load, the next loaded entry reloads and takes
them over. Their entities keep their entity ids and settings, and move to the
new entry's device.

```yaml
mentat:
  - name: Laundry
    # Entity ids, or a target with entity_id, device_id, area_id, floor_id, label_id.
    entities:
      - sensor.washing_machine_power
      - sensor.dryer_power
      - binary_sensor.laundry_door
    # Optional text or template, sent next to the readings as `note`.
    note: >-
      The washer idles under 5 W. It is {{ now().strftime('%A') }}.
    scan_interval: 300          # seconds, 1 at the least; default 300
    wake_on: []                 # ask when these change instead of the entities above
    with_attributes: false      # every attribute costs tokens on every request
    fire_event: true            # fire mentat_decision after each answer
    questions:
      - name: Washer done
        type: noul
        instructions: Has the washing machine finished its cycle?
        brief: A cycle ends with power under 5 W for two minutes.
        high: The cycle has finished and the drum is idle
        low: The machine is still washing, rinsing or spinning
        cutoff: 0.7
      - name: Laundry stage
        type: choice
        instructions: Where is the laundry right now?
        criteria:
          washing: The washer is running
          drying: The dryer is running
          waiting: A machine finished and nobody has opened its door
          idle: Neither machine has run recently
      - name: Urgency
        type: score
        instructions: How soon should someone move the laundry?
        criteria:
          - No need
          - Within a few hours
          - Within the hour
          - Now, before it creases
```

YAML reads an unquoted `yes`, `no`, `on` or `off` as true or false, so quote one
used as a word under `high`, `low` or a scale level, as in `high: "on"`. Mentat
refuses the unquoted form when it checks the configuration.

The question entities take the entry's name and the question's name, for example
`sensor.typesafe_jev_washer_done` and `binary_sensor.typesafe_jev_washer_done`.

`examples/` holds two worked setups: `house_mode.yaml`, a window watch with
three questions, and `package_check.yaml`, a porch package check with pictures.

## Windows of events

A plain watch judges how things are now. A window watch also judges what
happened over the last stretch of time. It keeps a log of state changes of its
entities and of the bus events you name, and sends that log with the readings.

```yaml
mentat:
  - name: House mode
    entities: [person.alex, person.sam, binary_sensor.hall_motion, binary_sensor.front_door]
    scan_interval: 600
    window:
      duration: "01:00:00"   # 1 minute to 24 hours
      max_events: 150        # 10 to 500, the oldest dropped first; default 100
      settle: 20             # seconds of quiet before judging, 0 to 300; default 2
      evaluate: both         # on_event, interval or both; default both
      events:
        - event_type: doorbell_pressed
          data: {button: front}   # matches events whose data holds these values
          label: someone rang the front doorbell
    questions:
      - name: Everyone asleep
        type: noul
        instructions: Has everyone at home gone to bed for the night?
        cutoff: 0.6
```

- `on_event` asks once the log has been quiet for `settle` seconds. Every change
  restarts the wait, so a burst of changes is one evaluation. A log that never
  goes quiet is judged three settle periods after the first change still
  waiting. `settle: 0` asks on every change. It has no schedule.
- `interval` keeps logging and asks only on `scan_interval`, which it needs.
- `both` does both.
- A window cannot log `mentat_decision` or any other `mentat_` event: the
  watch would ask again after each of its own answers.
- `with_attributes` and `collapse_repeats` also go under `window:`.
  `collapse_repeats` defaults to true and skips a change that repeats the state
  already logged.

The model receives the time, a summary of the window, the entities and the log.
Each entity carries how long it has held its state and how often it changed in
the window. A binary sensor also carries how long it was on and when it last was.
Mentat works these out so the model does not have to subtract timestamps.

At start the log is filled from the recorder, so a restart does not empty it. Bus
events are not in the recorder, so they start empty.

Every answered evaluation fires `mentat_decision`:

```yaml
entry_id: 01J...            # the Mentat entry
watch: house_mode           # the watch's name, slugified
watch_name: House mode
answers:
  house_mode_everyone_asleep:   # watch key and question name
    type: noul
    noul: 0.83
    name: Everyone asleep
    verdict: true               # only for a noul with a cutoff
window: {start: "...", end: "...", events: 42}   # null for a plain watch
model: jev-1.14.0
latency_ms: 274.0
input_tokens: 3120
```

A choice answer carries `choice`, `confidence` and `probabilities`. A score
answer carries `score`, `normalized`, `nearest_level`, `confidence`, `legend` and
`probabilities`. Trigger on the decision rather than on a sensor's every change:

```yaml
automation:
  - alias: Night scene when everyone is asleep
    triggers:
      - trigger: event
        event_type: mentat_decision
        event_data:
          watch: house_mode
    conditions:
      - "{{ trigger.event.data.answers.house_mode_everyone_asleep.verdict }}"
    actions:
      - action: scene.turn_on
        target:
          entity_id: scene.night
```

The question editor has the same settings in its Window of events section.

## Actions

Every action returns its answer as a response and needs `response_variable`.
Each takes `config_entry` when more than one Mentat entry is loaded, and `model`
to use another model for that one request. Give at least one of a target, `note`
or `pictures`.

### mentat.noul

```yaml
action: mentat.noul
target:
  entity_id: sensor.washing_machine_power
data:
  instructions: Has the washing machine finished?
  high: The cycle has finished
  low: It is still running
  cutoff: 0.7
response_variable: answer
```

Returns `noul`, `verdict`, `cutoff`, `model`, `latency_ms` and `usage` with
`input_tokens` and `output_tokens`. `verdict` is true once `noul` reaches
`cutoff`, which defaults to 0.5.

### mentat.choice

```yaml
action: mentat.choice
data:
  note: "{{ states('sensor.last_error_message') }}"
  instructions: Which part of the system does this error come from?
  options: [network, storage, power, other]
response_variable: answer
```

`options` is a list of names, or a mapping of each name to what it means. A name
with nothing after its colon has no description:

```yaml
  options:
    network: Wi-Fi, DNS or a router
    storage: A disk, a share or the database
    power:
    other:
```

Returns `choice`, `confidence`, `probabilities` and the same `model`,
`latency_ms` and `usage`.

### mentat.score

```yaml
action: mentat.score
target:
  area_id: garage
data:
  instructions: How urgent is what the garage sensors show?
  scale: [Ignore, This week, Today, Wake someone]
response_variable: answer
```

Returns `score`, `normalized` (the score divided by the top level),
`nearest_level`, `confidence`, `legend`, `probabilities`, `model`, `latency_ms`
and `usage`.

### mentat.ask

Several questions about one state in one request. Each question has a `type`
(`noul`, `choice`, `score` or `box`) and `instructions`; a choice or score takes
`criteria`. `window` reads the last stretch of time from the recorder for this
one call and needs a target.

```yaml
action: mentat.ask
target:
  entity_id: [binary_sensor.front_door, binary_sensor.hall_motion]
data:
  window:
    duration: "00:30:00"
  questions:
    someone_left:
      type: noul
      instructions: Did someone leave the house in this window?
    who:
      type: choice
      instructions: Who was it most likely?
      criteria: {alex: Alex, sam: Sam, nobody: Nobody left}
response_variable: answer
```

Returns `answers`, keyed as you named the questions, plus `model`, `latency_ms`
and `usage`.

### mentat.box

```yaml
action: mentat.box
data:
  picture: camera.driveway
  instructions: The parked car
response_variable: answer
```

Returns `left`, `top`, `right` and `bottom` as fractions of the picture from its
top left corner, `confidence`, and the source picture's `width` and `height` in
pixels. Multiply a fraction by the side to get pixels.

## Pictures

`pictures` takes up to four sources, fewer if the entry's options say so. A source
is a camera or image entity, or the full path of an image file Home Assistant may
read, such as a file under `/media` or in a folder listed in
`allowlist_external_dirs`. The state the model reads still lists them under
`images`.

Mentat turns each picture upright from its EXIF data, flattens transparency onto
white, resizes it for the dialect and sends it as a JPEG. The size it is sent at
is the size it is billed at, and its tokens join the budget estimate.

| Dialect | Sent at | Largest source | Billed |
| --- | --- | --- | --- |
| TypeSafe | Sides from 280 to 1568 px, 16 MiB | 50 megapixels | 2 tokens per 28 px block; a 1920 x 1080 frame is 3,584 |
| Clef | At most 1024 px and 1 megapixel, 4 MiB | 16 megapixels | 1 token per 32 px block plus 3, at most 1,024; the same frame is 579 |

A file larger than 50 MiB is refused without being read whole. `docs/measurements.md`
says where each figure comes from.

`box` works only on the TypeSafe dialect with Jev 1.14 or later. A Clef entry
refuses it before sending.

When a user calls an action from the UI or the API, a file path needs an
administrator, and a camera, image or target entity needs that user's permission
to read it. An automation is not limited, because each run starts without a
user. A script started by a user, from a dashboard button or with their token,
runs as that user and is checked like them.

So a dashboard button for a user who is not an administrator, running a script
that passes a file path or a camera outside their permissions, is refused. To
allow it, have the button call `automation.trigger` on an automation that makes
the call.

## AI Task

Each entry adds an AI Task entity named after the entry, for example
`ai_task.typesafe_jev`. The examples call it `ai_task.mentat`; rename yours or
change the id. `ai_task.generate_data` maps each field of the structure to a
question:

| Field selector | Question | Value returned |
| --- | --- | --- |
| `boolean` | noul | true at a probability of 0.5 or more |
| `select` | choice | the winning option |
| `number` with `min` and `max` | score on that scale | the weighted level, snapped to `step` |

A task without a structure, or with a text field or any other selector, is
refused before sending, because the model writes no prose. Image attachments,
camera snapshots included, go with the instructions. The result carries a
`mentat` key with the model, the input tokens, the latency, and each answer's
probability or confidence, so you can tell a sure answer from a guess. No field
may be called `mentat`.

## Cost and budget

Each entry has these sensors:

- Requests today, tokens today, and spend today, an estimate in US dollars.
- Over budget, a problem binary sensor.
- Per watch, latency and bytes diagnostic sensors, off by default. The bytes
  sensor reads the size of the last body and carries the exact state it sent as
  `sent_state`.

Input tokens are what you pay for, and the question text counts as input. Before
every request Mentat estimates its tokens from the body and the pictures and
refuses it if it would not fit `token_budget`. Scheduled watches, the
actions, the AI Task entity and the question preview all refuse the same way.
When the budget is spent, a repair issue says so and watches stop until
midnight. Raising the budget in the options clears it. An action with pictures is
refused before any camera is woken when not even the smallest request fits.

A watch nothing reads is not asked at all: one with `fire_event: false` whose
question, verdict and diagnostic entities are all disabled. A watch that
fires `mentat_decision` is always asked, because an automation may be reading
the event. Questions from the editor always fire it.

Hosted prices, per million input tokens: TypeSafe and OpenRouter $0.042,
Cloudflare Clef-flash $0.038 and Clef $0.24. Output is not billed.

## Limitations

- A decision model answers questions. It writes no text, summary or reply.
- Probabilities and confidences are not calibrated. Pick cutoffs by watching
  real answers for a while.
- The model is poor at arithmetic and at comparing timestamps. Work out numbers,
  durations and counts in a Jinja template and send the result.
- Do not let an answer unlock a door, disarm an alarm or do anything else where a
  wrong answer hurts someone.
- A request takes from a few hundred milliseconds to a few seconds, more with
  pictures. Mentat suits decisions that can wait that long.

## Troubleshooting

| What you see | Cause |
| --- | --- |
| Setup says "That key was refused" | Wrong key, or a Cloudflare token without Workers AI access. |
| Setup says the account "has run out of credit" | Top it up with the provider. |
| Setup says the server answered in another format | The dialect does not match the server, or the address has no `/v1/systemone` route. |
| "Its estimate is N tokens and only M of today's B remain" | The budget is spent for today. Raise it or set 0 in the options. |
| "The action has nothing to send" | The action has no target, `note` or `pictures`. |
| "Nothing by these names exists" | A device, area, floor or label in the target was removed or renamed. |
| "The target expands to N entities and one request may carry 250" | Every entity is billed on every request. Pick fewer, or split the watch. A window watch takes at most 100. |
| "Home Assistant may not read" a path | Add the folder to `allowlist_external_dirs`, or use a file under `/media`. |
| "Only an administrator may send a picture from a file path" | A user who is not an administrator called the action with a path. Use a camera or image entity. |
| "You do not have permission to read" an entity | The calling user cannot read that camera or entity. |
| "is too large to send" | The picture is over the dialect's source size, or the file is over 50 MiB. |
| "refused a box question" | The entry uses the Clef dialect, or a Jev model older than 1.14. |
| "which a decision model cannot fill: it returns no prose" | An AI Task structure has a text field or another selector Mentat cannot answer. Use boolean, select or number. |
| Question sensors are unavailable | The server is not answering or the budget is spent. The log and Repairs say which. |
| "A window logs changes to entities" | A window watch or `mentat.ask` with `window` needs entities as the target. |
| "A window cannot log Mentat's own events" | The window's bus events name `mentat_decision` or another `mentat_` event. Trigger an automation on the decision instead. |
| "so its levels would repeat" | An AI Task number field's `step` is larger than its range. Make the step smaller or leave it out. |
| "the reply's usage gives ... not a number" | The server answered without a token count the budget can add up. Check that the address and dialect point at a System One server. |

## Development

```sh
make venv       # creates .venv with uv and installs requirements-test.txt
make check      # ruff, ruff format --check, mypy --strict, pytest with coverage
make hassfest   # runs Home Assistant's hassfest in docker
```

The tests replace every client, so running them spends nothing. `make deploy`
copies the integration to a host as described under Install.
`scripts/make_brand.py` draws the icons and logos in
`custom_components/mentat/brand/`. `scripts/measure.py` sends real requests to a
server and prints the billed figures that `docs/measurements.md` records.
