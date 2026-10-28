// What the widget shows for each event on Rafiq's stream (docs/events.md). Run: cd widget && node --test
import assert from "node:assert/strict";
import { test } from "node:test";

import { busy, direction, ellipse, initial, label, now, reduce } from "../ui/model.js";
import { PHRASES, TAGS, t } from "../ui/phrases.js";

const connected = { kind: "connection", connected: true };
const play = (...events) => [connected, ...events].reduce(reduce, initial());
const started = { kind: "run", status: "started", run_id: "r1" };

test("a request starts a run with what was heard", () => {
  const s = play(started, { kind: "heard", text: "weather in Cairo", tag: "en", run_id: "r1" });
  assert.equal(s.run, "r1");
  assert.equal(s.request, "weather in Cairo");
});

test("steps are kept in order and updated as their status changes", () => {
  const s = play(started,
    { kind: "step", run_id: "r1", step_id: 1, name: "weather", status: "queued" },
    { kind: "step", run_id: "r1", step_id: 2, name: "web_search", status: "queued" },
    { kind: "step", run_id: "r1", step_id: 1, name: "weather", status: "running" },
    { kind: "step", run_id: "r1", step_id: 1, name: "weather", status: "done" });
  assert.deepEqual(s.steps.map((x) => [x.id, x.status]), [[1, "done"], [2, "queued"]]);
});

test("a new request clears the last one", () => {
  const s = play(started, { kind: "said", text: "Noon.", tag: "en", run_id: "r1" },
    { kind: "step", run_id: "r1", step_id: 1, name: "get_time", status: "done" },
    { kind: "run", status: "started", run_id: "r2" });
  assert.equal(s.run, "r2");
  assert.deepEqual([s.answer, s.steps], ["", []]);
});

test("a background task's events don't change the request on screen", () => {
  const s = play(started, { kind: "step", run_id: "task-ab12cd", step_id: 1, name: "web_search", status: "running" });
  assert.deepEqual(s.steps, []);
});

test("a plan, an explanation and cards are shown, the newest card first and at most four", () => {
  let s = play(started,
    { kind: "plan", title: "Book a table", steps: ["Find", "Call"], done: 1, run_id: "r1" },
    { kind: "explain", text: "First the loop.", tag: "en", run_id: "r1" });
  for (const city of ["a", "b", "c", "d", "e"]) s = reduce(s, { kind: "card", card: "weather", data: { city } });
  assert.deepEqual(s.plan, { title: "Book a table", steps: ["Find", "Call"], done: 1 });
  assert.equal(s.explain.text, "First the loop.");
  assert.deepEqual(s.cards.map((c) => c.data.city), ["e", "d", "c", "b"]);
});

test("another program's explanation says so", () => {
  const s = play({ kind: "explain", text: "Hi", tag: "en", source: "mcp" });
  assert.equal(s.explain.source, "mcp");
});

test("a question waiting for a yes is shown until it is answered", () => {
  let s = play(started, { kind: "confirm", action: "shut down the computer", tag: "en", always: false, run_id: "r1" });
  assert.deepEqual(s.confirm, { action: "shut down the computer", always: false });
  assert.equal(now(s), "shut down the computer?");
  s = reduce(s, { kind: "confirmed", yes: false, run_id: "r1" });
  assert.equal(s.confirm, null);
});

test("the small view shows the step running, then what Rafiq is doing", () => {
  let s = play(started, { kind: "state", state: "thinking" },
    { kind: "step", run_id: "r1", step_id: 1, name: "web_search", status: "running" });
  assert.equal(now(s), "web search");
  s = reduce(s, { kind: "step", run_id: "r1", step_id: 1, name: "web_search", status: "done" });
  assert.equal(now(s), t("state_thinking", "en"));
});

test("the small view says when Rafiq can't be reached, in the language on screen", () => {
  const s = play({ kind: "heard", text: "الجو عامل إيه", tag: "ar-EG" },
    { kind: "connection", connected: false, problem: "not_running" });
  assert.equal(now(s), PHRASES.not_running["ar-EG"]);
});

test("the widget says it is connecting until it is", () => {
  assert.equal(now(initial()), t("connecting", "en"));
});

test("a problem the page doesn't know is said as Rafiq stopping", () => {
  assert.equal(now(play({ kind: "connection", connected: false, problem: "io" })), t("stopped", "en"));
});

test("what the user asked Rafiq to forget leaves the screen", () => {
  const s = play(started, { kind: "heard", text: "my PIN is 1234", tag: "en", run_id: "r1" },
    { kind: "said", text: "Noted.", tag: "en", run_id: "r1" },
    { kind: "forgotten", texts: ["my PIN is 1234", "Noted."] });
  assert.deepEqual([s.request, s.answer], ["", ""]);
});

test("during a request, a satellite's or the phone's events don't replace it", () => {
  const s = play(started, { kind: "heard", text: "weather in Cairo", tag: "en", run_id: "r1" },
    { kind: "step", run_id: "r1", step_id: 1, name: "weather", status: "running" },
    { kind: "heard", text: "turn on the kitchen light", tag: "en", room: "kitchen" },
    { kind: "step", step_id: 1, name: "lights", status: "done" },
    { kind: "said", text: "Done.", tag: "en", room: "kitchen" });
  assert.equal(s.request, "weather in Cairo");
  assert.deepEqual(s.steps.map((x) => x.name), ["weather"]);
  assert.equal(s.answer, "");
});

test("during a request, another program's explanation is still shown", () => {
  const s = play(started, { kind: "explain", text: "Hi", tag: "en", source: "mcp" });
  assert.equal(s.explain.text, "Hi");
});

test("every phrase is written in all six languages", () => {
  for (const [key, table] of Object.entries(PHRASES)) assert.deepEqual(Object.keys(table).sort(), [...TAGS].sort(), key);
});

test("Arabic is shown right to left, and the other languages left to right", () => {
  assert.equal(direction("ar"), "rtl");
  assert.equal(direction("ar-EG"), "rtl");
  for (const tag of ["en", "fr", "es", "de", undefined]) assert.equal(direction(tag), "ltr");
});

test("the language on screen follows the request", () => {
  const s = play(started, { kind: "heard", text: "الجو عامل إيه", tag: "ar-EG", run_id: "r1" });
  assert.equal(s.tag, "ar-EG");
});

test("a tool name reads as words", () => {
  assert.equal(label("set_timer"), "set timer");
});

test("an event it doesn't know leaves the state as it was", () => {
  const before = play(started);
  assert.deepEqual(reduce(before, { kind: "level", level: 0.2 }), before);
});

test("a step's result is shown under it, and a private one only as hidden", () => {
  const s = play(started,
    { kind: "step", run_id: "r1", step_id: 1, name: "weather", status: "running" },
    { kind: "tool", run_id: "r1", step_id: 1, name: "weather", args: {}, result: "31°C, sunny" },
    { kind: "step", run_id: "r1", step_id: 1, name: "weather", status: "done" },
    { kind: "step", run_id: "r1", step_id: 2, name: "email_read", status: "running" },
    { kind: "tool", run_id: "r1", step_id: 2, name: "email_read", args: "(hidden)", result: "(hidden)" });
  assert.deepEqual(s.steps.map((x) => [x.status, x.result, x.hidden]),
    [["done", "31°C, sunny", false], ["running", null, true]]);
});

test("a long result is cut", () => {
  const s = play(started, { kind: "step", run_id: "r1", step_id: 1, name: "web_search", status: "done" },
    { kind: "tool", run_id: "r1", step_id: 1, name: "web_search", result: "x".repeat(900) });
  assert.equal(s.steps[0].result.length, 160);
});

test("Rafiq is busy while it thinks, speaks or runs a step, so stop is offered", () => {
  assert.equal(busy(play({ kind: "state", state: "idle" })), false);
  assert.equal(busy(play({ kind: "state", state: "speaking" })), true);
  assert.equal(busy(play(started, { kind: "step", run_id: "r1", step_id: 1, name: "x", status: "running" })), true);
});

test("stop is offered while Rafiq waits for an answer, since stopping then answers no", () => {
  const s = play({ kind: "state", state: "listening" }, started,
    { kind: "step", run_id: "r1", step_id: 1, name: "power", status: "asking" },
    { kind: "confirm", action: "shut down", tag: "en", always: false, run_id: "r1" });
  assert.equal(busy(s), true);
});

test("Always is offered only when Rafiq may remember the answer", () => {
  const ask = (always) => play(started, { kind: "confirm", action: "x", tag: "en", always, run_id: "r1" }).confirm.always;
  assert.deepEqual([ask(true), ask(false), ask(undefined)], [true, false, false]);
});

test("a screenshot with its marks is shown, and its picture added when it comes", () => {
  const marks = [{ n: 1, label: "the dialog", box: [100, 100, 300, 200], shape: "circle" }];
  let s = play(started, { kind: "screen", id: "a1", width: 640, height: 400, text: "Close [1].", marks, run_id: "r1" });
  assert.deepEqual(s.screen, { id: "a1", width: 640, height: 400, marks, src: null });
  s = reduce(s, { kind: "screen-image", id: "old", src: "data:image/png;base64,AAA" });
  assert.equal(s.screen.src, null);
  s = reduce(s, { kind: "screen-image", id: "a1", src: "data:image/png;base64,BBB" });
  assert.equal(s.screen.src, "data:image/png;base64,BBB");
});

test("a picture that isn't a data: PNG is never shown", () => {
  let s = play(started, { kind: "screen", id: "a1", width: 10, height: 10, marks: [], run_id: "r1" });
  s = reduce(s, { kind: "screen-image", id: "a1", src: "https://example.com/x.png" });
  assert.equal(s.screen.src, null);
});

test("a new request clears the screenshot", () => {
  const s = play(started, { kind: "screen", id: "a1", width: 10, height: 10, marks: [], run_id: "r1" },
    { kind: "run", status: "started", run_id: "r2" });
  assert.equal(s.screen, null);
});

test("a mark's circle goes around its box, a little larger", () => {
  assert.deepEqual(ellipse([100, 100, 300, 200]), { cx: 200, cy: 150, rx: 115, ry: 65 });
});

test("forgetting takes the screenshot off the screen", () => {
  const s = play(started, { kind: "screen", id: "a1", width: 10, height: 10, marks: [], run_id: "r1" },
    { kind: "forgotten", texts: [], kinds: ["screen"] });
  assert.equal(s.screen, null);
});

test("while Rafiq listens to a video, the small view says so with the time, and stop is offered", () => {
  let s = play({ kind: "state", state: "idle" }, { kind: "video", state: "listening", seconds: 95 });
  assert.equal(now(s), t("video_listening", "en") + " 1:35");
  assert.equal(busy(s), true);
  s = reduce(s, { kind: "video", state: "explaining" });
  assert.equal(now(s), t("video_explaining", "en"));
  s = reduce(s, { kind: "video", state: "done", why: "ended" });
  assert.equal(s.video, null);
  assert.equal(busy(s), false);
});

test("a video listened to outside a request is shown during one", () => {
  const s = play(started, { kind: "video", state: "listening", seconds: 30 });
  assert.equal(s.video.state, "listening");
});

test("losing Rafiq drops a video it was listening to, since a new Rafiq knows nothing of it", () => {
  const s = play({ kind: "video", state: "listening", seconds: 90 },
    { kind: "connection", connected: false, problem: "stopped" });
  assert.equal(s.video, null);
});
