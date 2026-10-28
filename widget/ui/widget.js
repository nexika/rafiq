// Draws what model.js says, as text only: nothing from the stream is ever read as HTML.
import { busy, direction, ellipse, initial, label, now, reduce } from "./model.js";
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

const SVG = "http://www.w3.org/2000/svg";

function svg(tag, attrs) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

/** One mark on the screenshot: its shape, and a numbered badge at its corner. Sizes follow the picture's. */
function mark(m, width, height) {
  const [left, top, right, bottom] = m.box;
  const line = Math.max(2, width / 320), badge = Math.max(10, width / 60);
  const group = svg("g", { class: "mark" });
  let at = [left, top];                // where the number goes: the corner, or an arrow's tail
  if (m.shape === "box") {
    group.append(svg("rect", { x: left, y: top, width: right - left, height: bottom - top, rx: line * 2,
      "stroke-width": line, class: "area" }));
  } else if (m.shape === "arrow") {
    // From up and to the left, or from below when the box is at the top edge, to the box's corner.
    const x0 = Math.max(badge, left - width / 10);
    const y0 = top - width / 10 >= badge ? top - width / 10 : Math.min(bottom + width / 10, height - badge);
    const angle = Math.atan2(top - y0, left - x0), head = line * 5;
    const wing = (a) => `${left - head * Math.cos(angle + a)},${top - head * Math.sin(angle + a)}`;
    group.append(svg("line", { x1: x0, y1: y0, x2: left, y2: top, "stroke-width": line }),
      svg("polygon", { points: `${left},${top} ${wing(0.45)} ${wing(-0.45)}`, class: "head" }));
    at = [x0, y0];
  } else {
    const e = ellipse(m.box);
    group.append(svg("ellipse", { cx: e.cx, cy: e.cy, rx: e.rx, ry: e.ry, "stroke-width": line }));
  }
  const number = svg("text", { x: at[0], y: at[1], "font-size": badge * 1.2, "text-anchor": "middle",
    "dominant-baseline": "central" });
  number.textContent = String(m.n);
  group.append(svg("circle", { cx: at[0], cy: at[1], r: badge, class: "badge" }), number);
  return group;
}

function drawScreen() {
  const shot = state.screen;
  $("screen").hidden = !shot || !shot.src;
  if (!shot || !shot.src) return;
  if ($("shot-img").getAttribute("src") !== shot.src) $("shot-img").setAttribute("src", shot.src);
  $("shot-img").alt = t("screenshot", state.tag);
  const layer = $("shot-marks");
  layer.setAttribute("viewBox", `0 0 ${shot.width} ${shot.height}`);
  layer.replaceChildren(...shot.marks.map((m) => mark(m, shot.width, shot.height)));
  $("shot-legend").replaceChildren(...shot.marks.map((m) => el("li", m.label, { value: m.n, dir: "auto" })));
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
  $("steps").replaceChildren(...state.steps.map((x) => {
    const item = el("li", null, { "data-status": x.status });
    const body = el("span", null, { class: "step" });
    body.append(el("span", label(x.name)));
    const result = x.hidden ? t("hidden", state.tag) : x.result;
    if (result) body.append(el("small", result, { dir: "auto" }));
    item.append(body);
    return item;
  }));
  $("confirm").hidden = !state.confirm;
  if (state.confirm) {
    $("confirm-text").textContent = state.confirm.action + "?";
    $("always").hidden = !state.confirm.always;
  }
  for (const id of ["yes", "no", "always", "send"]) $(id).textContent = t(id, state.tag);
  $("ask-text").placeholder = t("ask_placeholder", state.tag);
  $("stop").hidden = !busy(state);
  $("stop").title = $("stop").ariaLabel = t("stop", state.tag);
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
  drawScreen();
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

// The Rust side sends these to the running Rafiq with its token; a refusal comes back as a problem code.
let noticeTimer;
function call(command, args) {
  return tauri.core.invoke(command, args).then(() => true, (code) => {
    $("notice").textContent = t(code, state.tag) || t("stopped", state.tag);
    $("notice").hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { $("notice").hidden = true; }, 4000);
    return false;
  });
}

$("yes").addEventListener("click", () => call("answer", { yes: true, always: false }));
$("always").addEventListener("click", () => call("answer", { yes: true, always: true }));
$("no").addEventListener("click", () => call("answer", { yes: false, always: false }));
$("stop").addEventListener("click", () => call("stop", {}));
$("ask").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("ask-text").value.trim();
  if (!text) return;
  // Cleared once Rafiq has it: when it can't be sent, the text stays to send again.
  call("ask", { text }).then((sent) => { if (sent && $("ask-text").value.trim() === text) $("ask-text").value = ""; });
});

$("size").addEventListener("click", () => {
  tauri.core.invoke("set_large", { large: document.body.dataset.view === "small" });
});
window.addEventListener("resize", fitView);
fitView();
draw();

if (tauri) {
  tauri.event.listen("rafiq-event", (e) => update(e.payload));
  tauri.event.listen("rafiq-screen", (e) => update({ kind: "screen-image", ...e.payload }));
  tauri.event.listen("rafiq-connection", (e) => update({ kind: "connection", ...e.payload }));
}
