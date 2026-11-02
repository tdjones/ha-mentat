"""Constants. Every limit carries the measurement or the document that sized it."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "mentat"

# Entry data. CONF_API_KEY, CONF_URL and CONF_MODEL come from homeassistant.const.
CONF_PROVIDER: Final = "provider"
CONF_ACCOUNT_ID: Final = "account_id"
CONF_DIALECT: Final = "dialect"
CONF_IMAGE_FORMAT: Final = "image_format"
CONF_EXTRA: Final = "extra"
CONF_TRUNCATE: Final = "truncate"
# The collapsed form section holding the address, model, dialect and image format.
CONF_ADVANCED: Final = "advanced"

# Entry options.
CONF_TOKEN_BUDGET: Final = "token_budget"
CONF_TOKEN_PRICE: Final = "token_price"
CONF_MAX_IMAGES: Final = "max_images"

# The setup probe: one short noul against a two letter state. clef-flash billed it
# 143 input tokens (docs/measurements.md, text requests), which at TypeSafe's price
# is under a thousandth of a cent. TypeSafe's own count is an open row there.
PROBE_STATE: Final = "ok"
PROBE_KEY: Final = "probe"
PROBE_INSTRUCTIONS: Final = "Is this text in English?"

# The request path both dialects use when the address carries no {model} template
# (jevclient's SYSTEMONE_PATH, and the route clef_mlx.py serves).
SYSTEMONE_PATH: Final = "/v1/systemone"

# Seconds a request may take: jevclient's own DEFAULT_TIMEOUT, used for both dialects.
REQUEST_TIMEOUT_SECONDS: Final = 30.0

# The shortest schedule a watch may have. There is no rate floor beyond it: a
# decision model answers in well under a second, the daily token budget is what
# guards the spend, and a watch never runs two evaluations at once, so a
# request that arrives during one becomes a single follow-up.
MIN_SCAN_INTERVAL_SECONDS: Final = 1
# Five minutes between evaluations unless a watch says otherwise. A choice, not
# measured.
DEFAULT_SCAN_INTERVAL_SECONDS: Final = 300
# Seconds after a wake_on entity changes before a plain watch is asked, so the
# readings that change together land in one request. A choice, not measured.
TRIGGER_DEBOUNCE_SECONDS: Final = 1.0

# Entities one watch may send. A record billed 35.5 tokens on clef-flash
# (docs/measurements.md, text requests), so 250 records are about 8,900 tokens, or
# $0.0004 at TypeSafe's price if its tokenizer counts alike.
MAX_TARGET_ENTITIES: Final = 250
# A window watch also sends an event log, so it takes fewer entities. The ha-clef
# house mode replay (house_mode_replay_v2.py) watches 20; 100 is a ceiling chosen
# with room to spare, not a measurement.
MAX_WINDOW_ENTITIES: Final = 100
# Events kept per window. A choice, not measured: a log row is shorter than an
# entity record, so 500 rows should bill no more than the entity cap does.
MAX_WINDOW_EVENTS: Final = 500
MIN_WINDOW_EVENTS: Final = 10
DEFAULT_WINDOW_EVENTS: Final = 100

# Images per request. The ha-clef package check (package_replay.py) sends three, a
# baseline and two frames; four leaves room for one more and keeps a call bounded.
MAX_IMAGES: Final = 4
DEFAULT_MAX_IMAGES: Final = 4
# Seconds to wait for a camera or image entity's picture: camera.async_get_image's
# own default.
SNAPSHOT_TIMEOUT_SECONDS: Final = 10
# A file source is read whole before it is decoded, so a path pointed at a video by
# mistake must not fill memory. A 48 MP phone JPEG is about 15 to 25 MB; nothing
# past 50 MiB is a still worth sending.
MAX_SOURCE_FILE_BYTES: Final = 50 * 1024 * 1024
# A choice, not measured. Both dialects bill by pixels, so quality changes only the
# upload size, and a 1024 x 768 frame at 90 stays far under Clef's 4 MiB.
IMAGE_JPEG_QUALITY: Final = 90

# Every request is billed a fixed part beyond its body. clef-flash's fitted fixed
# part is 88 tokens (docs/measurements.md, text requests). Set a little below it:
# a fixed part guessed too high makes the learned ratio too high, and then a body
# much larger than the one it learned from is under-estimated. On clef-flash's
# line, 250 with a ratio learned from a 500-token call puts a 250-record body 25%
# short, past the margin. Below the true figure the error is on the safe side.
REQUEST_OVERHEAD_TOKENS: Final = 80
# Body bytes per billed token before any call has been measured. clef-flash bills
# 3.90 (docs/measurements.md, text requests); 2.8 sits below it because a low ratio
# over-estimates, which is the safe side. The ledger learns the real ratio from the
# first call big enough, so another tokenizer calibrates in one request. Bodies are
# measured as Home Assistant's compact encoder writes them.
COLD_START_BYTES_PER_TOKEN: Final = 2.8
# A call teaches the ratio only when its body was billed at least the fixed part.
# Below that, rounding in the fixed part swings the ratio, and a ratio learned from a
# probe-sized call would over-estimate every real request after it.
MIN_MEASURED_BODY_TOKENS: Final = REQUEST_OVERHEAD_TOKENS
# Estimates are padded by this much before the budget check. On clef-flash two sends
# of one body billed the same and the fitted line missed by 3 tokens at most
# (docs/measurements.md, text requests), so 20% is room for another server's fixed
# part and tokenizer. TypeSafe's spread is an open row there.
BUDGET_ESTIMATE_MARGIN: Final = 1.2

# The repair issue an entry raises once its daily budget is spent. The id carries the
# entry id so two entries never share one issue.
ISSUE_OVER_BUDGET: Final = "over_budget"

# Usage totals are written this long after a change, so a burst of calls makes one
# write. Unload flushes, so nothing recorded is lost to the delay.
STORE_SAVE_DELAY_SECONDS: Final = 15
STORAGE_VERSION: Final = 1

# Question fields, one spelling in YAML, the actions and the question editor. A noul's
# high and low say what each end of its probability means; jevclient's Noul takes
# them as true= and false=.
TYPE_NOUL: Final = "noul"
TYPE_CHOICE: Final = "choice"
TYPE_SCORE: Final = "score"
TYPE_BOX: Final = "box"
CONF_TYPE: Final = "type"
CONF_INSTRUCTIONS: Final = "instructions"
CONF_BRIEF: Final = "brief"
CONF_CRITERIA: Final = "criteria"
CONF_HIGH: Final = "high"
CONF_LOW: Final = "low"
CONF_CUTOFF: Final = "cutoff"

# Watch fields. CONF_NAME and CONF_SCAN_INTERVAL come from homeassistant.const.
CONF_QUESTIONS: Final = "questions"
CONF_NOTE: Final = "note"
CONF_ENTITIES: Final = "entities"
CONF_WITH_ATTRIBUTES: Final = "with_attributes"
CONF_WAKE_ON: Final = "wake_on"
CONF_FIRE_EVENT: Final = "fire_event"
CONF_WINDOW: Final = "window"

# Window fields, inside `window:` in YAML and in the action.
CONF_DURATION: Final = "duration"
CONF_MAX_EVENTS: Final = "max_events"
CONF_SETTLE: Final = "settle"
CONF_EVALUATE: Final = "evaluate"
CONF_EVENTS: Final = "events"
CONF_COLLAPSE_REPEATS: Final = "collapse_repeats"
CONF_EVENT_TYPE: Final = "event_type"
CONF_EVENT_DATA: Final = "data"
CONF_LABEL: Final = "label"
EVALUATE_ON_EVENT: Final = "on_event"
EVALUATE_INTERVAL: Final = "interval"
EVALUATE_BOTH: Final = "both"
EVALUATE_MODES: Final = (EVALUATE_ON_EVENT, EVALUATE_INTERVAL, EVALUATE_BOTH)
# The shortest window worth a call and the longest the recorder backfill is
# expected to read in one go (planning notes, 2026-10-30; not measured).
MIN_WINDOW_SECONDS: Final = 60
MAX_WINDOW_SECONDS: Final = 24 * 60 * 60
# Seconds of quiet a window waits for after its last entry before it is judged,
# so a door opening, a light coming on and a motion sensor firing land in one
# evaluation. Every entry restarts the wait. A choice, not measured.
DEFAULT_WINDOW_SETTLE_SECONDS: Final = 2.0
# A sensor that never goes quiet would put the evaluation off forever, so the
# wait ends this many settle periods after the first entry still waiting.
WINDOW_SETTLE_CAP_FACTOR: Final = 3
# Five minutes: past that the schedule decides when a window is judged, not the
# quiet. YAML and the editor share the cap.
MAX_WINDOW_SETTLE_SECONDS: Final = 300
# A day: the longest schedule the editor offers. YAML takes any whole second.
MAX_SCAN_INTERVAL_SECONDS: Final = 86400

# The question editor: one subentry per question. Its options and scale fields are
# multiline text boxes: `name: description` per line, and one level per line.
SUBENTRY_QUESTION: Final = "question"
CONF_TARGET: Final = "target"
SECTION_WINDOW: Final = "window"
CONF_WINDOW_ENABLED: Final = "window_enabled"
CONF_WINDOW_DURATION: Final = "window_duration"
CONF_WINDOW_MAX_EVENTS: Final = "window_max_events"
CONF_WINDOW_SETTLE: Final = "window_settle"
CONF_WINDOW_EVALUATE: Final = "window_evaluate"
CONF_WINDOW_EVENTS: Final = "window_events"
CONF_WINDOW_COLLAPSE_REPEATS: Final = "window_collapse_repeats"
DEFAULT_EDITOR_WINDOW_MINUTES: Final = 30

# Actions.
SERVICE_NOUL: Final = "noul"
SERVICE_CHOICE: Final = "choice"
SERVICE_SCORE: Final = "score"
SERVICE_ASK: Final = "ask"
SERVICE_BOX: Final = "box"
# A list of names, or a mapping of name to description.
CONF_OPTIONS: Final = "options"
CONF_SCALE: Final = "scale"
# The action fields only. The state the model reads keeps the key `images`.
CONF_PICTURES: Final = "pictures"
CONF_PICTURE: Final = "picture"
ATTR_CONFIG_ENTRY: Final = "config_entry"
ATTR_QUESTIONS: Final = "questions"
ATTR_ANSWERS: Final = "answers"
ATTR_USAGE: Final = "usage"
ATTR_LATENCY_MS: Final = "latency_ms"

# Entity attributes.
ATTR_CONFIDENCE: Final = "confidence"
ATTR_PROBABILITIES: Final = "probabilities"
ATTR_LEGEND: Final = "legend"
ATTR_NEAREST_LEVEL: Final = "nearest_level"
ATTR_PROBABILITY: Final = "probability"
ATTR_QUESTION_TYPE: Final = "question_type"
ATTR_SENT_STATE: Final = "sent_state"
# A window watch's question and verdict entities also say which window the answer
# is about.
ATTR_WINDOW_START: Final = "window_start"
ATTR_WINDOW_END: Final = "window_end"
ATTR_EVENT_COUNT: Final = "event_count"
ATTR_LAST_EVALUATED: Final = "last_evaluated"
WINDOW_ATTRIBUTES: Final = frozenset(
    {ATTR_WINDOW_START, ATTR_WINDOW_END, ATTR_EVENT_COUNT, ATTR_LAST_EVALUATED}
)

# Unique id parts, joined with colons by coordinator.unique_id. The platforms build
# ids from them, and nobody_reads() and the tests look the same ids up. A question
# is <scope>:<question>, its verdict <scope>:<question>:verdict, and a watch's
# diagnostics <scope>:watch:<watch>:<suffix>. The scope is the entry id, or
# YAML_ID_SCOPE for a YAML watch. The ledger's and the AI Task entity's are
# <entry>:<suffix>.
YAML_ID_SCOPE: Final = "yaml"
UID_WATCH: Final = "watch"
UID_VERDICT: Final = "verdict"
UID_LATENCY: Final = "latency"
UID_BYTES: Final = "bytes"
UID_REQUESTS: Final = "requests_today"
UID_TOKENS: Final = "tokens_today"
UID_SPEND: Final = "spend_today"
UID_OVER_BUDGET: Final = "over_budget"
UID_AI_TASK: Final = "ai_task"

# Fired on the bus after every answered evaluation of a watch. A window may not
# log this integration's own events: a watch judging its own decision would
# ask again as fast as the model answers.
EVENT_DECISION: Final = "mentat_decision"
OWN_EVENT_PREFIX: Final = f"{DOMAIN}_"
