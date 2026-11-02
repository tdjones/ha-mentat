# Measurements

This file backs every number in `const.py` and `providers.py`. A row is one of
three things: measured here, with the model and the date; read from a named
document; or a choice that nobody has measured, which the row says.

`scripts/measure.py` produces the measured rows. It builds the client the
integration builds, sends real requests and prints the rows below.

```sh
.venv/bin/python -m scripts.measure --url http://127.0.0.1:8000 \
    --dialect clef --model clef-flash --no-truncate
MENTAT_API_KEY=... .venv/bin/python -m scripts.measure \
    --url https://api.typesafe.ai --dialect typesafe --model jev-1.14.0
```

## Text requests

The script sends the setup probe, then a state of 0, 5, 10 and 20 entity records
twice each. Each record has the shape `readings.async_records` produces, with
generic ids. Every sized request carries the same noul. It fits billed input
tokens against `body_size()` by least squares as tokens = fixed part + bytes /
ratio, the line the ledger estimates with.

| Figure | Value | Model | Date | Backs |
| --- | --- | --- | --- | --- |
| Setup probe, billed | 143 tokens | clef-flash | 2026-11-01 | The `PROBE_*` comment |
| Fixed part per request, fitted | 88 tokens | clef-flash | 2026-11-01 | `REQUEST_OVERHEAD_TOKENS` = 80 |
| Body bytes per billed token, fitted | 3.90 | clef-flash | 2026-11-01 | `COLD_START_BYTES_PER_TOKEN` = 2.8 |
| Largest miss of the fit | 3.1 tokens | clef-flash | 2026-11-01 | `BUDGET_ESTIMATE_MARGIN` = 1.2 |
| Spread between two sends of one body | 0.0% | clef-flash | 2026-11-01 | `BUDGET_ESTIMATE_MARGIN` = 1.2 |
| Tokens per entity record | 35.5 | clef-flash | 2026-11-01 | `MAX_TARGET_ENTITIES` = 250 |
| Cold-start estimate for the 20 record body | 1,401 tokens against 871 billed | clef-flash | 2026-11-01 | The cold start errs high |
| Setup probe, billed | 273 tokens | jev-1.14.0 | 2026-11-01 | The `PROBE_*` comment |
| Fixed part per request, fitted | 211 tokens | jev-1.14.0 | 2026-11-01 | `REQUEST_OVERHEAD_TOKENS` = 80 |
| Body bytes per billed token, fitted | 2.19 | jev-1.14.0 | 2026-11-01 | `COLD_START_BYTES_PER_TOKEN` = 2.2 |
| Largest miss of the fit | 5.0 tokens | jev-1.14.0 | 2026-11-01 | `BUDGET_ESTIMATE_MARGIN` = 1.2 |
| Spread between two sends of one body | 0.0% | jev-1.14.0 | 2026-11-01 | `BUDGET_ESTIMATE_MARGIN` = 1.2 |
| Tokens per entity record | 63.4 | jev-1.14.0 | 2026-11-01 | `MAX_TARGET_ENTITIES` = 250 |
| Cold-start estimate for the 20 record body | 1,393 tokens against 1,596 billed | jev-1.14.0 | 2026-11-01 | The estimate at 2.8 came in 13% under, so the ratio moved to 2.2 |

The fixed part depends on the provider, 88 tokens on clef-flash and 211 on
TypeSafe. Two sends of one body billed the same both times on both.

Receipts from the clef-flash run, served by clef_mlx.py on the same machine with
`truncate: false`:

| Call | Body bytes | Input tokens | Output tokens |
| --- | --- | --- | --- |
| probe | 132 | 143 | 0 |
| 0 records, twice | 267 | 160 | 0 |
| 5 records, twice | 1,002 | 343 | 0 |
| 10 records, twice | 1,686 | 518 | 0 |
| 20 records, twice | 3,043 | 871 | 0 |

Receipts from the TypeSafe run, jev-1.14.0, 2026-11-01. The sends of one body
billed the same; latency is the time each took.

| Call | Body bytes | Input tokens | Output tokens | Latency ms |
| --- | --- | --- | --- | --- |
| probe | 115 | 273 | 20 | 171 |
| 0 records, run 1 | 250 | 329 | 21 | 73 |
| 0 records, run 2 | 250 | 329 | 21 | 72 |
| 5 records, run 1 | 985 | 656 | 21 | 85 |
| 5 records, run 2 | 985 | 656 | 21 | 75 |
| 10 records, run 1 | 1,669 | 973 | 21 | 71 |
| 10 records, run 2 | 1,669 | 973 | 21 | 100 |
| 20 records, run 1 | 3,026 | 1,596 | 21 | 78 |
| 20 records, run 2 | 3,026 | 1,596 | 21 | 89 |

The fixed part was 250 before this run. A fixed part set above the true one makes
the ledger learn too high a ratio, because it credits too few tokens to the body.
On clef-flash's line, a ratio learned from a 500-token call at 250 puts a
250-record body at 6,829 tokens against 9,054 billed, 25% short and past the 20%
margin. At 80 the same case over-estimates, and a body of nothing is estimated at
96 tokens against 88. So the default moved to 80, a little under the fit. TypeSafe's fixed part is 211,
so 80 is under it too, and the same reasoning holds: the ledger credits the gap
to the body, learns a low ratio and errs high.

The cold-start ratio was 2.8. On TypeSafe's line it estimated the 20 record body
at 1,393 tokens against 1,596 billed, 13% short even after the 1.2 margin. At 2.2
the same body is estimated at 1,747, 9% over. On clef-flash, which bills 3.90
bytes per token, 2.2 estimates the 20 record body at 1,756 against 871, high and
on the safe side.

Body bytes are counted with Home Assistant's `json_bytes`, the compact UTF-8
encoder both clients post with. That is what `DecisionClient.body_size()` returns.

## Pictures

The script sends one 280 x 280 and one 1024 x 768 gradient JPEG through
`pictures.prepare_image`, each followed by the same body with no picture. The
difference between the two is the picture's bill.

| Figure | Value | Model | Date | Backs |
| --- | --- | --- | --- | --- |
| Picture sent at 280 x 280 | 84 tokens, as `image_tokens()` says | clef-flash | 2026-11-01 | Clef's formula |
| Picture sent at 1024 x 768 | 771 tokens, as `image_tokens()` says | clef-flash | 2026-11-01 | Clef's formula |
| Picture sent at 280 x 280 | 202 tokens, formula says 200 (text part sent alone) | jev-1.14.0 | 2026-11-01 | TypeSafe's formula |
| Picture sent at 1024 x 768 | 2,074 tokens, formula says 2,072 (text part sent alone) | jev-1.14.0 | 2026-11-01 | TypeSafe's formula |

On TypeSafe the formula is within 2 tokens at both sizes. The 2 are the asset
reference text that comes with a picture.

| Dialect | Figure | Value | Source |
| --- | --- | --- | --- |
| TypeSafe | Side of a sent picture | 280 to 1568 px | jevclient 1.3.1's `MIN_IMAGE_SIDE` and `MAX_IMAGE_SIDE`, taken from docs.typesafe.ai/images. The library notes that the API refuses a picture outside them. |
| TypeSafe | Bytes per picture | 16 MiB | jevclient 1.3.1's `MAX_IMAGE_BYTES`. |
| TypeSafe | Largest source | 50 MP | A choice. A 48 MP phone photo fits, and a decoded 50 MP picture takes about 150 MB. Refused before decoding. |
| TypeSafe | Tokens per picture | `2 * ceil(w/28) * ceil(h/28)` | Billed within 2 tokens of this at 280 x 280 and 1024 x 768 on jev-1.14.0, 2026-11-01. The 2 are the asset reference text. |
| Clef | Longest side sent | 1024 px | Past 1024 px the 32 px blocks pass the 1024 token cap, so more pixels cost upload time and buy nothing. |
| Clef | Pixels sent | at most 1 MP | Planning notes, 2026-10-30. clef-flash took 1024 x 768. |
| Clef | Bytes per picture | 4 MiB | Planning notes, 2026-10-30. Not measured. A larger encode shrinks by a quarter per side until it fits. |
| Clef | Largest source | 16 MP | Planning notes, 2026-10-30. Not measured. Refused before decoding. |
| Clef | Tokens per picture | `min(1024, ceil(w/32) * ceil(h/32) + 3)` | Cloudflare's Clef model page, read 2026-10-30, for the blocks and the cap. clef-flash billed exactly this, the 3 included. |
| Both | Pictures per request | 4 | ha-clef's `package_replay.py` sends three, a reference and two frames. The fourth is spare. Lowered per entry in the options. |
| Both | JPEG quality | 90 | A choice. Billing goes by pixels, so quality changes only the upload size. |
| Both | Decompression bomb | refused as too large | Pillow warns past `MAX_IMAGE_PIXELS`, about 89 MP, and raises past twice that. Both are above every dialect's cap. |
| Both | Largest file read | 50 MiB | A choice. A 48 MP phone JPEG is 15 to 25 MB, and a path pointed at a video by mistake is not read whole. |
| Both | Snapshot timeout | 10 s | `camera.async_get_image`'s own default. |

## What common sources cost

The formulas above applied to pictures after resizing. The Clef column is
measured at two sizes, and so is the TypeSafe column, to within 2 tokens.

| Source | TypeSafe sends | TypeSafe tokens | Clef sends | Clef tokens |
| --- | --- | --- | --- | --- |
| 1920 x 1080 camera frame | 1568 x 882 | 3,584 | 1024 x 576 | 579 |
| 640 x 480 still | 640 x 480 | 828 | 640 x 480 | 303 |
| 4032 x 3024 phone photo | 1568 x 1176 | 4,704 | 1024 x 768 | 771 |

## Choices with no measurement behind them

| Constant | Value | Why |
| --- | --- | --- |
| `DEFAULT_SCAN_INTERVAL_SECONDS` | 300 | A starting point. Each watch and each question in the editor sets its own. |
| `TRIGGER_DEBOUNCE_SECONDS` | 1 | Readings that change together land in one request. |
| `DEFAULT_WINDOW_SETTLE_SECONDS` | 2 | A door, a light and a motion sensor that fire together are judged together. |
| `MAX_WINDOW_ENTITIES` | 100 | ha-clef's house mode replay watches 20. The window log adds tokens on top. |
| `MAX_WINDOW_EVENTS` | 500 | A log row is shorter than an entity record, so 500 rows stay under the entity cap's bill. |
| `MIN_WINDOW_SECONDS`, `MAX_WINDOW_SECONDS` | 60, 86,400 | Planning notes, 2026-10-30. |
| `REQUEST_TIMEOUT_SECONDS` | 30 | jevclient's `DEFAULT_TIMEOUT`, used for both dialects. |
| Brief folded into instructions | | `watches.fold_brief` assumes the model weighs a rule in the question more than one in the readings. Nobody has measured it. |
