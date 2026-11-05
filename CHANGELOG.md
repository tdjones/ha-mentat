# Changelog

## Unreleased

- A noul's verdict can hold between two numbers. Give the question a `release`
  under its `cutoff`, in YAML or as "Verdict turns off below" in the editor: the
  verdict turns on at or over the cutoff, off under the release, and keeps its
  last state in between, so an answer that wobbles around the cutoff no longer
  flips it on every evaluation. The verdict sensor and `mentat_decision` say
  `held` when it did. A question without a release behaves as before; nothing
  needs changing.
- `mentat.ask` takes a `cutoff` on a noul and returns its `verdict`, as
  `mentat.noul` does. It used to ignore a `cutoff` it did not use, and now
  refuses one on a choice or score, one outside 0 to 1, and any `release`. A call
  that carried one of those stops with an error naming the question; remove the
  key.
- `mentat.calibrate` suggests a noul's `cutoff` and `release` from the
  recorder. Give it the noul's sensor and an entity that shows what was really
  true, such as a door contact. It replays the history through the verdict and
  returns the numbers that did best, what they would have caught, missed and
  flipped, and whether the answers overlap so much that the question needs
  rewording. It sends nothing and costs no tokens.
- A score sensor carries `normalized`, the score over its top level, so scores
  with scales of different lengths can be weighted together in a template.

## 0.1.0 (2026-11-01)

The first release. Not dated until the release commit.

- Connects to TypeSafe Jev, OpenRouter, Cloudflare Workers AI (Clef and
  Clef-flash), a local Clef server, or any System One server in either dialect.
  Setup sends one probe question, so a wrong key, address or dialect fails
  before the entry is saved.
- Asks noul, choice and score questions about your entities, a note or both. A
  question is written in YAML as part of a watch under `mentat:`, or in the
  question editor, which runs one trial before saving. Questions that read the
  same thing share one request.
- A window watch sends a log of state changes and bus events over the last
  stretch of time, filled from the recorder at start, and asks once the log goes
  quiet for `settle` seconds. Every answer fires `mentat_decision`.
- Five actions return their answers: `mentat.noul`, `mentat.choice`,
  `mentat.score`, `mentat.ask` and `mentat.box`. Each takes up to four pictures
  from cameras, image entities or allowed files, resized and billed for the
  dialect.
- An AI Task entity answers boolean, select and number fields and refuses a task
  with a text field before sending it.
- A daily token budget per entry, with requests, tokens and spend sensors, an
  over-budget binary sensor and a repair issue. Every request path refuses a
  request that would not fit before sending it. The estimate starts from 80
  fixed tokens per request and 2.2 body bytes per token, and relearns the ratio
  from each large enough call. The 80 sits under the fixed part
  measured on both providers in `docs/measurements.md`, 88 on clef-flash and 211
  on TypeSafe, so the estimate errs high. The 2.2 is TypeSafe's measured 2.19.
  The first guess of 2.8 estimated a 20 record body 13% under its bill there.
- A person calling an action may send only the entities, cameras and files they
  can read, and a file path needs an administrator. Credentials, picture links
  and addresses are stripped from attributes and event data at every depth.
- One entry runs the YAML watches. If it is disabled, removed or fails to load,
  the next loaded entry takes them over, and their entities keep their ids.
- Setup asks every watch's first question at the same time. A setup that fails
  stops every watch it started.
- Removing an entry deletes its stored day of tokens and its over-budget repair.

What to do: nothing. There is no earlier version to move from.
