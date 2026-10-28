# Rafiq's event stream

Rafiq tells the panel, and any client built apart from it (the widget), what is happening as it
happens. This page is the contract: what a client can rely on, and every kind of event.

## Reading the stream

`GET http://127.0.0.1:<port>/events?token=<token>` is a
[server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html) stream. The
panel listens on `127.0.0.1` only, checks the Host header, and refuses a wrong or missing token
with 403.

When a client connects, it gets the last 100 kept events, then where things stand now (`state`,
`privacy`, `mode`, `persona`), then each new event as it happens. A line starting with `:` every
15 seconds keeps the connection open; skip it.

Each `data:` line is one JSON object:

```json
{"kind": "said", "time": 1793180400.12, "version": 1, "run_id": "3f9c2a1b", "text": "It's noon.", "tag": "en"}
```

| Field | Always there | Meaning |
|---|---|---|
| `kind` | yes | What happened, from the list below. |
| `time` | yes, except the four sent on connecting | Unix time in seconds. |
| `version` | yes | The stream's version, now `1`. |
| `run_id` | only inside a request or a background task | Groups one request's events. |

## What stays the same

- Fields and kinds are only ever added within a version. A client must ignore what it doesn't know.
- Removing or renaming a field or kind, or changing what one means, raises `version`.
- Private tools never show their arguments or results: `tool` events say `"(hidden)"`, and `step`
  events never carry arguments or results at all.

## Runs and steps

A **run** is one request, from what was heard or typed to the answer. Every event published while
it lasts carries the same `run_id`, including the spoken yes to a confirmation. A run starts and
ends with a `run` event:

```json
{"kind": "run", "status": "started", "run_id": "3f9c2a1b", "time": 1793180399.80, "version": 1}
{"kind": "run", "status": "done", "run_id": "3f9c2a1b", "time": 1793180401.02, "version": 1}
```

`status` ends as `done`, or `failed` when the request raised an error; a failed run says which in
`error`, for example `"error": "TimeoutError: timed out"`.

A **step** is one tool the model called. `step_id` counts the tool calls of one request from 1,
in the order the model asked for them. A step's `step` event comes again each time its status changes:

| Status | When |
|---|---|
| `queued` | The model asked for it; it hasn't started. Several tools asked at once are all queued first. |
| `asking` | Waiting for the user's yes. |
| `running` | The tool is running. |
| `done` | It finished. |
| `failed` | It returned an error, raised one, or doesn't exist. |
| `declined` | The user said no; it didn't run. |

```json
{"kind": "step", "run_id": "3f9c2a1b", "step_id": 1, "name": "weather", "status": "running", "started": 1793180400.01, "time": 1793180400.01, "version": 1}
{"kind": "step", "run_id": "3f9c2a1b", "step_id": 1, "name": "weather", "status": "done", "started": 1793180400.01, "ended": 1793180400.43, "time": 1793180400.43, "version": 1}
```

`started` is when the model asked for the tool, the same on every event of the step. `ended` comes
with `done`, `failed` and `declined`. Every step ends: one left open when a turn stops early is
ended as `failed`. Both are Unix times. The
`tool_start`, `waiting` and `tool` events of a step carry its `step_id` too.

A **background task** runs apart from the request that started it, so its `task` events carry
their own run: `"run_id": "task-<id>"`.

Not in a run yet: requests from a satellite speaker (`heard` and `said` with a `room`) and from the
phone bridge. Their `step`, `tool_start` and `tool` events have no `run_id`, so two of them at once
can't be told apart.

## Every kind

The fields shown are besides `kind`, `time`, `version` and `run_id`.

### The request

| Kind | Fields | Example |
|---|---|---|
| `run` | `status`: `started`, `done`, `failed`; `error` when failed | `{"status": "started"}` |
| `heard` | `text`, `tag`; `room` from a satellite | `{"text": "weather in Cairo", "tag": "en"}` |
| `typed` | `text` | `{"text": "weather in Cairo"}` |
| `step` | `step_id`, `name`, `status`, `started`, `ended` | see above |
| `tool_start` | `name`, `step_id` | `{"name": "weather", "step_id": 1}` |
| `waiting` | `name`, `text` (what Rafiq says while a slow tool runs), `tag`, `step_id` | `{"name": "web_search", "text": "Let me look.", "tag": "en", "step_id": 2}` |
| `tool` | `name`, `args`, `result` (first 500 characters, or `declined`), `card`, `step_id` | `{"name": "weather", "args": {"city": "Cairo"}, "result": "31°C, sunny", "card": "weather", "step_id": 1}` |
| `confirm` | `action`, `tag`, `always` (whether "yes, always" may be remembered) | `{"action": "shut down the computer", "tag": "en", "always": true}` |
| `confirmed` | `yes` | `{"yes": false}` |
| `said` | `text`, `tag`; `cards` when there are some; `room` from a satellite | `{"text": "It's 31 degrees.", "tag": "en"}` |
| `scene` | `id`, `steps`: `[tool, result]` pairs | `{"id": "film", "steps": [["lights", "dimmed"]]}` |
| `task` | `id`, `goal`, `state` (`running`, `done`, `failed`), `cost`, `result` | `{"id": "ab12cd", "run_id": "task-ab12cd", "goal": "find a flight", "state": "running", "cost": null, "result": ""}` |
| `brain` | `backend`; `mode` and `model` when changed in the panel | `{"backend": "claude"}` |
| `spending` | `today`, `month`, `daily_cap`, `monthly_cap`, `currency`, `status` | `{"today": 0.012, "month": 0.41, "daily_cap": 1, "monthly_cap": 10, "currency": "USD", "status": "ok"}` |
| `error` | `error`, or `brain` (each backend's error), or `satellite` | `{"error": "TimeoutError: timed out"}` |

### Listening and speaking

| Kind | Fields | Example |
|---|---|---|
| `state` | `state`: `idle`, `listening`, `thinking`, `speaking`, `dictating`, … | `{"state": "thinking"}` |
| `wake` | `score` and `threshold` (recordings), or `text` (Whisper) | `{"text": "Rafiq"}` |
| `voice` | `score`, `threshold`, `match` (the voice print) | `{"score": 0.21, "threshold": 0.3, "match": true}` |
| `level` | `level` (microphone meter; not kept) | `{"level": 0.04}` |
| `calibrated` | `threshold`, `end_seconds`, `noise_rms`, `rate` | `{"threshold": 0.5, "end_seconds": 0.8, "noise_rms": 0.0031, "rate": 16000}` |
| `interrupted` | none | `{}` |
| `muted` | `muted` | `{"muted": true}` |
| `sleep` | none | `{}` |
| `notify` | `text`, `event`, `spoken` | `{"text": "Tea is ready.", "event": "timer", "spoken": true}` |
| `dictation` | `on`, `tag` | `{"on": true, "tag": "en"}` |
| `dictated` | `text`, `tag` | `{"text": "Dear Sara,", "tag": "en"}` |
| `reading` | `index`, `total`, `state` | `{"index": 3, "total": 12, "state": "reading"}` |
| `interpret` | `on`, `languages` | `{"on": true, "languages": ["en", "ar-EG"]}` |
| `interpreted` | `text`, `source`, `translation`, `target` | `{"text": "hello", "source": "en", "translation": "أهلاً", "target": "ar-EG"}` |
| `practice` | `state` (`start` or `end`), `tag`; `level` and `topic`, or `summary` | `{"state": "start", "tag": "fr", "level": "beginner", "topic": "food"}` |
| `correction` | `said`, `correct`, `note`, `tag` | `{"said": "je suis faim", "correct": "j'ai faim", "note": "avoir faim", "tag": "fr"}` |

### Settings and the computer

| Kind | Fields | Example |
|---|---|---|
| `mode` | `mode` | `{"mode": "everyday"}` |
| `persona` | `persona` | `{"persona": "butler"}` |
| `privacy` | `brain`, `voice`, `cloud`, `offline` | `{"brain": "claude", "voice": "edge", "cloud": ["claude", "edge"], "offline": false}` |
| `settings` | `settings` (what the panel may change) | `{"settings": {"speech": {"follow_up_seconds": 6}}}` |
| `project` | `path` | `{"path": "/home/you/code/app"}` |
| `presence` | `state`: `away` or `home` | `{"state": "away"}` |
| `paper` | `result` | `{"result": "printed 2 pages"}` |
| `model` | `action`, `name`, `ok`, `result` | `{"action": "activate", "name": "whisper small", "ok": true, "result": "active"}` |
| `gestures` | `on` | `{"on": true}` |
| `gesture` | `gesture`, `action` | `{"gesture": "palm", "action": "pause"}` |
| `satellite` | `name`, `room`, `connected` | `{"name": "kitchen-pi", "room": "kitchen", "connected": true}` |
| `phone` | `kind`, `text` | `{"kind": "note", "text": "buy bread"}` |
| `forgotten` | `texts` (remove these from what is shown; not kept) | `{"texts": ["what time is it"]}` |
