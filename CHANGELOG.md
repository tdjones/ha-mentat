# Changelog

## 0.2.0 (2026-11-23)

The first version on HACS. Mentat asks System One decision models typed questions
about your house and answers as sensors, action responses and events. What it
does is in the README; the changes below matter if you already run a copy of 0.1.0
installed by hand.

- An action sends its `note` as it arrives and never renders it again. A script
  or an automation renders its data before the call, so braces left in a note
  came from what it rendered, such as an MQTT payload or a webhook body, and
  rendering them again could read any entity. A note template in an automation
  or a script still works, rendered once by Home Assistant. One that arrives
  unrendered, as through the REST API, goes to the model as written; render it
  before you send it.
- An action's `model` must be an id the provider can take. With Cloudflare that
  is `clef-flash` or `clef`, as in setup, because the id is part of the address
  the key goes to. Any provider refuses an id with a space. A call that named
  another model stops with an error naming it.
- A key refused for a model an action named starts no reauthentication.
- A window cannot log `*` or `state_changed`. Each hears the whole house,
  Mentat's own answers among them. A YAML block with either is refused at start,
  and a refused block stops all of Mentat from loading, editor questions and
  actions too. Before you update, take any such filter out of YAML: give the
  watch entities for state changes and name the bus events. A question saved in the editor with either keeps loading, and its window
  no longer logs a Mentat event or a change to a Mentat entity.
- More attributes stay home: `code`, `pin`, `location`, `address`, `ip_address`,
  `mac_address`, `email`, `gps`, `coordinates` and `session` as whole keys, and
  any key containing `apikey`, `authorization`, `bearer`, `cookie`, `passcode`,
  `passphrase`, `passwd`, `private_key` or `webhook`. A value that is an address
  with a login or a query, such as a signed camera link, is dropped under any
  key. An object in an entity's attributes is read as plain data first, so the
  rules reach inside it. A question that relied on one of these attributes no
  longer sees it.
- A target that takes in an entity the caller may not read, through an area, a
  device, a label or a group, is refused without naming that entity.
- A Clef server's redirect fails the request instead of being followed, a reply
  over 1 MiB is not read, and a failure names the server without the path that
  holds the Cloudflare account id.
- A reply with a count of tokens below zero, or a number that is not finite such
  as NaN, is refused as unreadable instead of lowering the day's spend or leaving
  a verdict stuck.
- The editor's trial page keeps the state it shows inside its code block, whatever
  backticks the readings hold.

- Mentat installs through HACS. Add `https://github.com/tdjones/ha-mentat` as a
  custom repository of type Integration. HACS downloads into the same folder as
  a copy installed by hand, so entries and watches carry over.
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
  returns the calmest numbers that scored close to the best, what they would have
  caught, missed and flipped, and whether the answers overlap so much that nothing
  separates them. A `grace` allows for answers coming after what they describe. It
  sends nothing and costs no tokens.
- A choice or score can have a `min_confidence`. Under it a score reads unknown and
  a choice reads its `fallback`, or unknown without one, and the model's answer
  stays in the attributes with `confident` and `leaning`. A choice can give an option
  a higher bar of its own in YAML. `mentat_decision`, `mentat.choice`, `mentat.score`
  and `mentat.ask` add `confident` and keep the model's answer. A question without
  one behaves as before; nothing needs changing.
- A watch asks nothing until Home Assistant has started. It used to ask while other
  integrations were still loading, which logged an error for a watch whose entities
  had no state yet and billed an answer about part of the house for one whose
  entities had partly loaded. Question sensors stay unavailable a little longer
  after a restart.
- A score sensor carries `normalized`, the score over its top level, so scores
  with scales of different lengths can be weighted together in a template.

## 0.1.0 (2026-11-01)

The first version, never published as a release.

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
