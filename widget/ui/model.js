// What the widget shows, from Rafiq's event stream (docs/events.md). Pure functions, tested with node --test:
// the page only draws what reduce() returns.

import { t } from "./phrases.js";

const RTL = new Set(["ar", "ar-EG"]);
// Without a run_id these come from a satellite or the phone (docs/events.md), not from the request on screen.
const OF_THE_REQUEST = new Set(["heard", "typed", "step", "confirm", "confirmed", "said"]);
const MAX_CARDS = 4;

export function initial() {
  return { problem: "connecting", state: "idle", run: null, request: "", tag: "en", steps: [], plan: null,
    explain: null, cards: [], answer: "", confirm: null };
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
    return { ...s, request: gone.has(s.request) ? "" : s.request, answer: gone.has(s.answer) ? "" : s.answer };
  }
  switch (e.kind) {
    case "state": return { ...s, state: e.state };
    case "heard": case "typed": return { ...s, request: e.text, tag: e.tag || s.tag };
    case "step": {
      const step = { id: e.step_id, name: e.name, status: e.status };
      const known = s.steps.some((x) => x.id === e.step_id);
      return { ...s, steps: known ? s.steps.map((x) => (x.id === e.step_id ? step : x)) : [...s.steps, step] };
    }
    case "plan": return { ...s, plan: { title: e.title, steps: e.steps, done: e.done } };
    case "explain": return { ...s, explain: { text: e.text, tag: e.tag, source: e.source || null } };
    case "card": return { ...s, cards: [{ card: e.card, data: e.data }, ...s.cards].slice(0, MAX_CARDS) };
    case "confirm": return { ...s, confirm: e.action };
    case "confirmed": return { ...s, confirm: null };
    case "said": return { ...s, answer: e.text, tag: e.tag || s.tag };
    default: return s;
  }
}

/** One line for the small view: a problem, the question waiting, the step running, or the state. */
export function now(s) {
  if (s.problem) return t(s.problem, s.tag) || t("stopped", s.tag);
  if (s.confirm) return s.confirm + "?";
  const running = s.steps.find((x) => x.status === "running" || x.status === "asking");
  return running ? label(running.name) : t("state_" + s.state, s.tag) || s.state;
}

export function direction(tag) {
  return RTL.has(tag) ? "rtl" : "ltr";
}

export function label(name) {
  return String(name).replaceAll("_", " ");
}
