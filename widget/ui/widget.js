// Draws what model.js says, as text only: nothing from the stream is ever read as HTML.
import { direction, initial, label, now, reduce } from "./model.js";
import { t } from "./phrases.js";

const tauri = window.__TAURI__;
const $ = (id) => document.getElementById(id);
const SMALL_HEIGHT = 200;              // below this the window shows the small view

let state = initial();

function el(tag, text, attrs = {}) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function show(id, text) {
  $(id).hidden = !text;
  $(id).textContent = text || "";
}

function draw() {
  document.documentElement.dir = direction(state.tag);
  document.documentElement.lang = state.tag;
  document.body.dataset.state = state.state;
  if (state.problem) document.body.dataset.problem = ""; else delete document.body.dataset.problem;
  $("now").textContent = now(state);

  show("request", state.request);
  $("plan").hidden = !state.plan;
  if (state.plan) {
    $("plan-title").textContent = state.plan.title;
    $("plan-steps").replaceChildren(...state.plan.steps.map((text, i) =>
      el("li", text, i < state.plan.done ? { "data-done": "", dir: "auto" } : { dir: "auto" })));
  }
  $("steps").replaceChildren(...state.steps.map((x) => el("li", label(x.name), { "data-status": x.status })));
  show("confirm", state.confirm && state.confirm + "?");
  $("explain").hidden = !state.explain;
  if (state.explain) {
    $("explain-from").textContent = t(state.explain.source === "mcp" ? "from_program" : "rafiq_explains", state.tag);
    $("explain-text").textContent = state.explain.text;
  }
  $("cards").replaceChildren(...state.cards.map((c) => {
    const card = el("section", null, { class: "card" });
    const list = el("dl");
    for (const [k, v] of Object.entries(c.data)) list.append(el("dt", k, { dir: "auto" }), el("dd", v, { dir: "auto" }));
    card.append(el("h3", t("card_" + c.card, state.tag) || c.card), list);
    return card;
  }));
  show("answer", state.answer);
  fitView();
}

function update(event) {
  state = reduce(state, event);
  draw();
}

function fitView() {
  const small = window.innerHeight < SMALL_HEIGHT;
  document.body.dataset.view = small ? "small" : "large";
  $("size").textContent = small ? "⤢" : "⤡";
  $("size").title = $("size").ariaLabel = t(small ? "show_more" : "show_less", state.tag);
}

$("size").addEventListener("click", () => {
  tauri.core.invoke("set_large", { large: document.body.dataset.view === "small" });
});
window.addEventListener("resize", fitView);
fitView();
draw();

if (tauri) {
  tauri.event.listen("rafiq-event", (e) => update(e.payload));
  tauri.event.listen("rafiq-connection", (e) => update({ kind: "connection", ...e.payload }));
}
