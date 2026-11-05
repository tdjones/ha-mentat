# Mentat

Mentat is a Home Assistant custom integration, domain `mentat`, that asks System
One decision models typed questions about a house. It talks to TypeSafe's Jev,
Cloudflare's Clef and any server that speaks either dialect. An answer arrives as a sensor, an
action response, a `mentat_decision` event or an AI Task result. Runtime code is
in `custom_components/mentat`. The project lives on GitLab and is installed by
hand, so `CHANGELOG.md` is the only release note a user reads.

## Money

The user pays for every request by the input token.

- Every path that sends a request asks the ledger first and refuses one that
  would not fit the day's budget.
- Something that wakes a watch needs a schedule, a debounce or a settle wait
  that a flapping sensor cannot defeat. A watch never runs two requests at once.
- The suite spends nothing. The `fake_client` fixture patches `build_client`
  where setup and the config flow call it. A test that reaches a real server is
  a bug in the test.

## Answers an automation can trust

A question entity with no answer to its own question is unavailable. It never
shows a stale or made-up value. An entity that has its answer but whose own rules
cannot give a value yet, such as a verdict first answered between release and
cutoff, is unknown and stays available. `judge.py` holds those rules, so the
entities and the event read one judgement. Choice and score entities carry the model's
confidence as an attribute. A noul has no confidence attribute, because its
probability is already the whole answer.

## Privacy

- `readings.redact()` runs over attributes and event data at every depth.
  Credentials, picture links and addresses never reach a request.
- When a person calls an action, `services.py` checks that they may read every
  entity, camera and file the call would send. A file path needs an admin.
- Examples, tests and docs use addresses from `192.0.2.0/24` and generic ids such
  as `sensor.washing_machine_power`. The maintainer's hostnames, LAN addresses,
  rooms, people and keys never go in the repository.

## Where a change has to land

A fix that works on one path and is missing on another is the usual defect.
Before calling work done, go through these and say which applied.

- **Request paths.** The watch coordinator asks on a schedule, on a `wake_on`
  change and when a window settles. The five actions are `mentat.noul`,
  `choice`, `score`, `ask` and `box`. The AI Task entity answers
  `ai_task.generate_data`. The editor's trial asks once before saving. The last
  three reach the server through `services.async_ask`. `mentat.calibrate` reads
  the recorder and sends nothing, so it is no request path, and its tests check
  that nothing reaches the client.
- **Dialects.** `TypeSafeClient` and `ClefClient` in `client.py` each build and
  measure their own body. Picture limits and token formulas for both live in
  `providers.py`.
- **Config sources.** A watch comes from YAML through `schema.py` and
  `watch_from_yaml`, or from editor subentries through `watches_from_subentries`.
  A new setting goes into both, or the README says why not.
- **Forms.** The entry flow has user, account id, reauth and reconfigure steps
  and an options step. The editor's noul, choice and score forms share their
  sections with its reconfigure form.
- **Entities.** A new one gets a `translation_key`, an icon in `icons.json` and an
  entity category. Diagnostics ship disabled.

## User-facing text

- `strings.json` and `translations/en.json` are the same file twice.
  `tests/test_strings.py` fails when they differ, when code raises a
  `translation_key` with no text, or when `services.yaml` and the strings disagree
  on an action's fields.
- Raise `HomeAssistantError` and its subclasses with `translation_domain`,
  `translation_key` and `translation_placeholders`. Flow errors and abort reasons
  use keys under `config`. Trial sentences live under `common`.
- hassfest refuses a URL in `strings.json`. Pass one in a placeholder.
- The README describes what Mentat does now, in the present tense. History goes
  in commits.
- Headings are sentence case. No em dashes or en dashes as punctuation.
- A comment gives the reason, the number or the trap. The code says what it does.
- A number in the docs or in a `const.py` or `providers.py` comment cites a row of
  `docs/measurements.md`, or says it is a choice nobody has measured.

## Checking your work

```bash
make check      # ruff check, ruff format --check, mypy --strict, pytest at 100% coverage
make hassfest   # Home Assistant's hassfest in docker; amd64 only, so Apple silicon emulates it
```

`make venv` builds `.venv` with uv from `requirements-test.txt`, which pins what
Home Assistant 2026.9.4 pins for the components Mentat imports. CI fails under 95%
coverage; the tree sits at 100%.

The fixtures in `tests/conftest.py` are `fake_client`, `entry` (a config entry
not yet added), `loaded` (set up with no watches), `yaml_watch` (loads YAML
watches and their entry) and `FakeRecorder`. `tests/test_examples.py` loads each
file in `examples/` through the real schemas. A test checks what a user can see:
an entity state, a response, a form error, an event. When a test guards a
refusal, break the guard once and watch it fail.

`scripts/measure.py` talks to a real server and is never imported by the tests.
Its output goes into `docs/measurements.md`.

## Words

- **watch**: one request's worth of questions about one state. A YAML item under
  `mentat:` or a group of editor questions. Each has a coordinator.
- **question**: one subentry or one item under a watch's `questions`, and one
  sensor. Types are noul, choice, score and box.
- **note**: text or a template sent as the state, or beside the readings.
- **brief**: standing facts sent with a question instead of with the state.
- **high**, **low**: what each end of a noul's probability means.
- **cutoff**: the probability at which a noul's **verdict** turns true. The
  verdict is a binary sensor and a key in responses and events.
- **scale**: a score's levels, lowest first.
- **pictures**: the action field for cameras, image entities and files. The state
  the model reads still calls them `images`.
- **ledger**: one entry's day of billed tokens, in `budget.py`.
- **window**: a log of state changes and bus events sent beside the readings.
- **settle**: the quiet a window waits for before it is judged.
- **dialect**: the wire format. TypeSafe posts pictures as named assets; Clef
  posts an ordered list and may wrap replies in Cloudflare's envelope.

## Modules

- `__init__.py`: sets up an entry. Client, ledger, probe, one coordinator per watch.
- `providers.py`: provider presets, dialects, prices, picture limits, entry ids.
- `client.py`: speaks each dialect over HTTP and measures the body it would post.
- `budget.py`: the ledger, its estimates, in-flight holds and the repair issue.
- `readings.py`: target to sorted entity records, refusals, redaction.
- `window.py`: a window's log, the recorder backfill, a window watch's state.
- `watches.py`: frozen watch and question dataclasses and the shared question builder.
- `judge.py`: what an answer shows after its question's own rules, such as a held verdict.
- `schema.py`: the YAML schema, which refuses what the API would refuse.
- `pictures.py`: fetches, turns, sizes, encodes and bills pictures per dialect.
- `fields.py`: AI Task fields to questions, and answers back to field values.
- `ai_task.py`: the AI Task entity.
- `services.py`: the six actions, `async_ask` and the caller checks.
- `calibrate.py`: replays a noul's recorded history through its verdict. Sends nothing.
- `preview.py`: the editor's trial page, as markdown.
- `editor.py`: the question editor subentry flow.
- `config_flow.py`: entry setup, reauth, reconfigure and options.
- `coordinator.py`: one watch's request, the `mentat_decision` event, runtime data.
- `entity.py`: entity bases and the one service device per entry.
- `sensor.py`, `binary_sensor.py`: question, verdict, ledger and per-watch sensors.
- `diagnostics.py`, `system_health.py`: what a user pastes into an issue.
- `const.py`: names and limits, each limit with its source.
- `services.yaml`, `strings.json`, `icons.json`, `brand/`: what Home Assistant shows.

## Commits

- The subject is a plain sentence about what changed for the user, such as "A
  window watch waits for the house to go quiet before it asks".
- No attribution lines and no tool or assistant names.
- A release bumps `version` in `manifest.json` and dates the `CHANGELOG.md` entry
  in a commit named `Release x.y.z`. The entry says what changed for the user and
  what they must do, if anything.
- Push, open a merge request or comment on an issue only when the maintainer asks.
