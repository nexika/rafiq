// What the widget shows, from Rafiq's event stream (docs/events.md). Pure functions, tested with node --test:
// the page only draws what reduce() returns.

import { t } from "./phrases.js";

const RTL = new Set(["ar", "ar-EG"]);
// Without a run_id these come from a satellite or the phone (docs/events.md), not from the request on screen.
const OF_THE_REQUEST = new Set(["heard", "typed", "step", "confirm", "confirmed", "said"]);
const MAX_CARDS = 4;
const RESULT_CHARS = 160;
const HIDDEN = new Set(["(hidden)", "error (hidden)"]);   // what Rafiq sends for a private tool (brain.py)

export function initial() {
  return { problem: "connecting", state: "idle", run: null, request: "", tag: "en", steps: [], plan: null,
    explain: null, cards: [], answer: "", confirm: null, screen: null };
}

/** The state after one event. Events of another run (a background task), of a satellite or of the phone leave
 * the request on screen alone. */
export function reduce(s, e) {
  if (e.kind === "connection") return { ...s, problem: e.connected ? null : e.problem || "not_running" };
  if (e.kind === "run" && e.status === "started") return { ...initial(), problem: s.problem, state: s.state, run: e.run_id };
  if (e.run_id && s.run && e.run_id !== s.run) return s;
  if (e.room || (s.run && !e.run_id && OF_THE_REQUEST.has(e.kind))) return s;
  if (e.kind === "forgotten") {
    const gone = new Set(e.texts || []);
    return { ...s, request: gone.has(s.request) ? "" : s.request, answer: gone.has(s.answer) ? "" : s.answer,
      screen: (e.kinds || []).includes("screen") ? null : s.screen };
  }
  switch (e.kind) {
    case "state": return { ...s, state: e.state };
    case "heard": case "typed": return { ...s, request: e.text, tag: e.tag || s.tag };
    case "step": {
      const known = s.steps.find((x) => x.id === e.step_id);
      const step = { result: null, hidden: false, ...known, id: e.step_id, name: e.name, status: e.status };
      return { ...s, steps: known ? s.steps.map((x) => (x.id === e.step_id ? step : x)) : [...s.steps, step] };
    }
    case "tool": {
      if (e.step_id == null) return s;
      const hidden = HIDDEN.has(e.result);
      const result = hidden || typeof e.result !== "string" ? null : e.result.slice(0, RESULT_CHARS);
      return { ...s, steps: s.steps.map((x) => (x.id === e.step_id ? { ...x, result, hidden } : x)) };
    }
    case "plan": return { ...s, plan: { title: e.title, steps: e.steps, done: e.done } };
    case "explain": return { ...s, explain: { text: e.text, tag: e.tag, source: e.source || null } };
    case "screen": return { ...s, screen: { id: e.id, width: e.width, height: e.height, marks: e.marks || [], src: null } };
    case "screen-image": {
      // From the Rust side, which fetched it with the token: only a PNG data: URL for the screenshot on screen.
      const fits = s.screen && s.screen.id === e.id && typeof e.src === "string" && e.src.startsWith("data:image/png;");
      return fits ? { ...s, screen: { ...s.screen, src: e.src } } : s;
    }
    case "card": return { ...s, cards: [{ card: e.card, data: e.data }, ...s.cards].slice(0, MAX_CARDS) };
    case "confirm": return { ...s, confirm: { action: e.action, always: e.always === true } };
    case "confirmed": return { ...s, confirm: null };
    case "said": return { ...s, answer: e.text, tag: e.tag || s.tag };
    default: return s;
  }
}

/** One line for the small view: a problem, the question waiting, the step running, or the state. */
export function now(s) {
  if (s.problem) return t(s.problem, s.tag) || t("stopped", s.tag);
  if (s.confirm) return s.confirm.action + "?";
  const running = s.steps.find((x) => x.status === "running" || x.status === "asking");
  return running ? label(running.name) : t("state_" + s.state, s.tag) || s.state;
}

/** Whether there is something to stop: Rafiq thinking or speaking, a step running, or a question waiting. */
export function busy(s) {
  return ["thinking", "speaking"].includes(s.state) || Boolean(s.confirm)
    || s.steps.some((x) => x.status === "running" || x.status === "asking");
}

/** The ellipse drawn around a mark's box: centred on it, 15 pixels larger each way. */
export function ellipse([left, top, right, bottom]) {
  return { cx: (left + right) / 2, cy: (top + bottom) / 2, rx: (right - left) / 2 + 15, ry: (bottom - top) / 2 + 15 };
}

export function direction(tag) {
  return RTL.has(tag) ? "rtl" : "ltr";
}

export function label(name) {
  return String(name).replaceAll("_", " ");
}
