"""Interactive HTML companion for the VF review file (todo T8).

Rendered next to the review JSON when a job finishes: an offline, self-contained
dark-theme page where the owner reviews the parsed changes (chips per meaning,
partial accept, inline typo fixes) and downloads a ready apply-JSON in exactly
the format ``vf_file.parse_review_document`` reads back. The original JSON stays
the apply artifact of record; this page only *produces* one.

The layout/JS mirrors the owner-approved mockup (2026-07-11): field checkbox on
the right, "was" column hugging its content, 250ms single-click delay so a
double click can start chip editing, example split on newlines.
"""

import json

_DATA_PLACEHOLDER = "__VF_REVIEW_DATA__"

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>VF review</title>
<style>
  /* Dark theme, self-contained, offline. */
  :root {
    --bg: #16191d;
    --panel: #1e232a;
    --panel2: #242b33;
    --border: #333c46;
    --text: #cdd6e0;
    --muted: #7d8894;
    --accent: #4da3ff;
    --green: #3fb950;
    --green-bg: rgba(63,185,80,.13);
    --red: #f47067;
    --yellow: #d9a03f;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 18px 22px 60px;
    background: var(--bg); color: var(--text);
    font: 14px/1.45 system-ui, "Segoe UI", Roboto, sans-serif;
  }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: 12px; margin-bottom: 14px; }
  .topbar {
    position: sticky; top: 0; z-index: 5;
    background: var(--bg); padding: 10px 0 12px;
    border-bottom: 1px solid var(--border);
    display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
  }
  .counter { background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
             padding: 6px 12px; font-size: 13px; }
  .counter b { color: var(--accent); }
  .counter b.warn { color: var(--yellow); }
  select, input[type=search] {
    background: var(--panel2); color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 6px 10px; font-size: 13px; outline: none;
  }
  .spacer { flex: 1; }
  button.primary, button.ghost {
    border-radius: 8px; padding: 8px 16px; font-size: 13px; cursor: pointer;
    border: 1px solid var(--accent); font-weight: 600;
  }
  button.primary { background: var(--accent); color: #0c1116; }
  button.primary:hover { filter: brightness(1.12); }
  button.ghost { background: transparent; color: var(--accent); }
  .word {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    margin: 14px 0; overflow: hidden; max-width: 1250px;
  }
  .word.off { opacity: .45; }
  .whead {
    display: flex; align-items: center; gap: 10px; padding: 10px 14px;
    background: var(--panel2); border-bottom: 1px solid var(--border);
  }
  .whead .lemma { font-size: 16px; font-weight: 700; color: #e8eef5; }
  .badge {
    font-size: 11px; padding: 2px 8px; border-radius: 999px;
    border: 1px solid var(--border); color: var(--muted); text-transform: uppercase;
  }
  .badge.level { color: var(--green); border-color: var(--green); }
  .badge.status-pending { color: var(--yellow); border-color: var(--yellow); }
  .badge.status-error { color: var(--red); border-color: var(--red); }
  .whead label { margin-left: auto; font-size: 12px; color: var(--muted); cursor: pointer;
                 display: flex; gap: 6px; align-items: center; }
  .werr { padding: 8px 14px; color: var(--red); font-size: 12px; }
  table.fields { width: 100%; border-collapse: collapse; }
  table.fields td {
    padding: 9px 14px; border-top: 1px solid var(--border); vertical-align: top;
  }
  td.fname { width: 90px; color: var(--muted); font-size: 12px; white-space: nowrap; }
  td.fcheck { width: 40px; text-align: right; padding-right: 16px; }
  /* "was" hugs its content: the value column starts right after it regardless
     of how wide the window is stretched. */
  td.cur { width: 1%; min-width: 100px; max-width: 260px; color: var(--muted);
           padding-right: 14px; }
  td.cur .lbl, td.new .lbl { font-size: 10px; text-transform: uppercase; letter-spacing: .5px;
                             color: var(--muted); display: block; margin-bottom: 3px; }
  tr.rejected td.new { opacity: .35; }
  .chip {
    display: inline-flex; align-items: center; gap: 5px;
    margin: 2px 4px 2px 0; padding: 3px 10px;
    border-radius: 999px; border: 1px solid var(--border); background: #2b323b;
    cursor: pointer; user-select: none; font-size: 13px;
  }
  .chip.new { background: var(--green-bg); border-color: var(--green); color: #7ee787; }
  .chip.offc { background: transparent; border-style: dashed; color: var(--muted);
               text-decoration: line-through; }
  .chip.drag-source { opacity: .18; pointer-events: none; }
  .chip.drop-target { outline: 2px solid var(--accent); outline-offset: 1px; }
  .chip.drag-ghost {
    position: fixed; z-index: 1000; margin: 0; pointer-events: none;
    opacity: .94; transform: scale(1.04) rotate(-1deg);
    box-shadow: 0 10px 28px rgba(0,0,0,.42);
  }
  .drag-handle {
    border: 0; padding: 0; background: transparent; color: inherit;
    font: inherit; line-height: 1; cursor: grab; opacity: .65; touch-action: none;
  }
  .drag-handle:active { cursor: grabbing; }
  .drag-handle:hover, .drag-handle:focus-visible { opacity: 1; }
  .drag-handle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .chip input { background: var(--panel2); color: var(--text); border: 1px solid var(--accent);
                border-radius: 6px; font-size: 13px; padding: 1px 6px; }
  .add-chip {
    width: 28px; height: 28px; margin: 2px 4px 2px 0; padding: 0;
    border-radius: 999px; border: 1px dashed var(--accent);
    background: transparent; color: var(--accent); cursor: pointer;
    font-size: 18px; line-height: 24px; vertical-align: middle;
  }
  .add-chip:hover { background: rgba(77,163,255,.12); }
  .add-chip:focus-visible, .add-chip-input:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 2px;
  }
  .add-chip-input {
    width: 220px; margin: 2px 4px 2px 0; padding: 4px 8px;
    border-radius: 6px; border: 1px solid var(--accent);
    background: var(--panel2); color: var(--text); font-size: 13px;
    outline: none; vertical-align: middle;
  }
  .single {
    display: inline-block; min-width: 60px; padding: 3px 10px; border-radius: 6px;
    border: 1px solid var(--green); background: var(--green-bg); color: #7ee787; cursor: text;
  }
  .single.block { display: block; white-space: pre-line; max-width: 360px; }
  h2.section { font-size: 14px; color: var(--yellow); margin: 26px 0 4px; }
  input[type=checkbox] { accent-color: var(--accent); width: 15px; height: 15px; cursor: pointer; }
  #toast {
    position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%);
    background: var(--accent); color: #0c1116; font-weight: 600;
    padding: 10px 22px; border-radius: 10px; display: none;
  }
</style>
</head>
<body>

<h1 id="title">🌐 VF review</h1>
<div class="sub">Клик по чипу — включить/выключить значение · двойной клик — исправить текст · перетяни перевод за «⋮⋮» — изменить порядок · «+» — добавить перевод · галка справа — принять/отклонить поле · галка в шапке слова — слово целиком. Закончив, жми «Скачать apply-JSON» и отправь файл боту.</div>

<div class="topbar">
  <span class="counter" id="cWords"></span>
  <span class="counter" id="cFields"></span>
  <span class="counter" id="cPending"></span>
  <select id="fType"><option value="">тип: все</option></select>
  <select id="fField"><option value="">поле: все</option></select>
  <input type="search" id="fSearch" placeholder="поиск слова…">
  <span class="spacer"></span>
  <button class="ghost" onclick="copyJson()">Скопировать JSON</button>
  <button class="primary" onclick="downloadJson()">⬇ Скачать apply-JSON</button>
</div>

<div id="list"></div>

<h2 class="section" id="pendTitle" style="display:none">⚠ Не обработано (ошибки / pending) — в apply не попадут</h2>
<div id="pending"></div>

<div id="toast">Скопировано ✔</div>

<script>
// Review document injected by the bot at render time.
const DATA = __VF_REVIEW_DATA__;

// Fields whose value is a list of meanings separated by ", "
const COMMA_FIELDS = new Set(["russian", "ukrainian", "english"]);
// example: one chip per sentence, split on newlines
const NL_FIELDS = new Set(["example"]);
// Multiline blocks (owner 11.07): shown/edited as a whole — the Präsens block
// must stay a valid 6-line unit, so no per-line chips.
const BLOCK_FIELDS = new Set(["conjugation_praesens"]);
// Nullable integer status: null = unknown, 0 = no, 1 = yes.
const TRI_STATE_FIELDS = new Set(["is_separable"]);
// Field order used to synthesize a diff for ADD jobs (new cards have no diff).
const CARD_FIELDS = ["type", "article", "german", "plural", "infinitive", "prateritum",
  "partizip2", "komparativ", "superlativ", "russian", "ukrainian", "english",
  "example", "cefr_level", "conjugation_praesens", "is_separable"];

const norm = s => s.trim().toLowerCase();
const splitVal = (field, v) =>
  (v || "").split(NL_FIELDS.has(field) ? "\n" : ",").map(s => s.trim()).filter(Boolean);

// ---- Build the editable model from DATA ----
const model = DATA.words.map((w, i) => {
  const r = w._review || {};
  let diff = r.diff || {};
  // ADD job: a fresh card has no diff — treat every filled field as new-vs-empty
  // so it renders (and can be partially accepted) the same way as a recheck.
  if (!Object.keys(diff).length && r.status === "done") {
    diff = {};
    for (const f of CARD_FIELDS) {
      if (TRI_STATE_FIELDS.has(f) && Object.prototype.hasOwnProperty.call(w, f))
        diff[f] = { current: null, vf: w[f] };
      else if (String(w[f] || "").trim())
        diff[f] = { current: "", vf: String(w[f]) };
    }
  }
  const isPending = r.status !== "done" || !Object.keys(diff).length;
  const fields = Object.entries(diff).map(([fname, d]) => {
    if (TRI_STATE_FIELDS.has(fname)) {
      return { name: fname, kind: "triState", current: triStateValue(d.current),
        value: triStateValue(d.vf), accepted: true };
    }
    if (BLOCK_FIELDS.has(fname)) {
      return { name: fname, kind: "block", current: d.current, value: d.vf, accepted: true };
    }
    if (COMMA_FIELDS.has(fname) || NL_FIELDS.has(fname)) {
      const curVals = splitVal(fname, d.current);
      const curSet = new Set(curVals.map(norm));
      const chips = curVals.map(t => ({ text: t, on: true, isNew: false }));
      for (const t of splitVal(fname, d.vf))
        if (!curSet.has(norm(t))) chips.push({ text: t, on: true, isNew: true });
      return { name: fname, kind: "chips", current: d.current, chips, accepted: true };
    }
    return { name: fname, kind: "single", current: d.current, value: d.vf, accepted: true };
  });
  return { idx: i, word: w, review: r, isPending, accepted: !isPending, fields };
});

function lemmaOf(m) {
  return m.review.headword || m.word.german || m.word.infinitive || m.review.query || "?";
}

let shownFields = 0;

function render() {
  const type = document.getElementById("fType").value;
  const field = document.getElementById("fField").value;
  const q = norm(document.getElementById("fSearch").value || "");
  const list = document.getElementById("list");
  const pend = document.getElementById("pending");
  list.innerHTML = ""; pend.innerHTML = "";
  shownFields = 0;

  for (const m of model) {
    if (type && (m.word.type || "") !== type) continue;
    if (q && !norm(lemmaOf(m)).includes(q)) continue;

    const card = document.createElement("div");
    card.className = "word" + (m.accepted ? "" : " off");

    const head = document.createElement("div");
    head.className = "whead";
    const status = m.review.status || "";
    head.innerHTML =
      `<span class="lemma"></span>` +
      (m.word.type ? `<span class="badge">${esc(m.word.type)}</span>` : "") +
      (m.word.cefr_level ? `<span class="badge level">${esc(m.word.cefr_level)}</span>` : "") +
      ((m.review.flags || []).length ? `<span class="badge status-pending">⚠ ${esc((m.review.flags || []).join(","))}</span>` : "") +
      (m.isPending ? `<span class="badge status-${status === "error" ? "error" : "pending"}">${esc(status || "pending")}</span>` : "");
    head.querySelector(".lemma").textContent = lemmaOf(m);
    const lab = document.createElement("label");
    lab.innerHTML = `<input type="checkbox" ${m.accepted && !m.isPending ? "checked" : ""} ${m.isPending ? "disabled" : ""}> слово в apply`;
    lab.querySelector("input").onchange = e => setWordAccepted(m, card, e.target.checked);
    head.appendChild(lab);
    card.appendChild(head);

    if (m.isPending && m.review.error) {
      const err = document.createElement("div");
      err.className = "werr"; err.textContent = m.review.error;
      card.appendChild(err);
    }

    if (!m.isPending) {
      const tbl = document.createElement("table");
      tbl.className = "fields";
      for (const f of m.fields) {
        if (field && f.name !== field) continue;
        shownFields++;
        const tr = document.createElement("tr");
        if (!f.accepted) tr.className = "rejected";

        const tdN = document.createElement("td"); tdN.className = "fname";
        tdN.textContent = f.name;

        const tdCur = document.createElement("td"); tdCur.className = "cur";
        tdCur.innerHTML = `<span class="lbl">было</span>`;
        tdCur.appendChild(multiline(f.kind === "triState" ? triStateLabel(f.current) : (f.current || "—")));

        const tdNew = document.createElement("td"); tdNew.className = "new";
        tdNew.innerHTML = `<span class="lbl">станет</span>`;
        if (f.kind === "triState") {
          const sel = document.createElement("select");
          sel.innerHTML = '<option value="unknown">неизвестно</option>' +
            '<option value="0">нет</option>' +
            '<option value="1">да</option>';
          wireTriState(sel, f);
          tdNew.appendChild(sel);
        } else if (f.kind === "chips") {
          f.chips.forEach(ch => tdNew.appendChild(makeChip(f, ch)));
          if (COMMA_FIELDS.has(f.name)) tdNew.appendChild(makeAddChip(f));
        } else if (f.kind === "block") {
          // multiline unit (Präsens conjugation): edited as a whole, lines kept
          const dv = document.createElement("div");
          dv.className = "single block"; dv.contentEditable = "true";
          dv.textContent = f.value;
          wireEditable(dv, f, true);
          tdNew.appendChild(dv);
        } else {
          const sp = document.createElement("span");
          sp.className = "single"; sp.contentEditable = "true"; sp.textContent = f.value;
          wireEditable(sp, f, false);
          tdNew.appendChild(sp);
        }

        const tdC = document.createElement("td"); tdC.className = "fcheck";
        const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = f.accepted;
        cb.onchange = () => setFieldAccepted(f, tr, cb.checked);
        tdC.appendChild(cb);

        tr.append(tdN, tdCur, tdNew, tdC);
        tbl.appendChild(tr);
      }
      card.appendChild(tbl);
    }
    (m.isPending ? pend : list).appendChild(card);
  }
  document.getElementById("pendTitle").style.display =
    model.some(m => m.isPending) ? "" : "none";
  updateCounters();
}

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function multiline(text) {
  const frag = document.createDocumentFragment();
  String(text).split("\n").forEach((line, i) => {
    if (i) frag.appendChild(document.createElement("br"));
    frag.appendChild(document.createTextNode(line));
  });
  return frag;
}

function setWordAccepted(m, card, accepted) {
  m.accepted = accepted;
  card.classList.toggle("off", !accepted);
  updateCounters();
}

function setFieldAccepted(f, row, accepted) {
  f.accepted = accepted;
  row.classList.toggle("rejected", !accepted);
  updateCounters();
}

function setChipEnabled(ch, el, enabled) {
  ch.on = enabled;
  el.classList.toggle("offc", !enabled);
  updateCounters();
}

function triStateValue(value) {
  return value === 0 || value === 1 ? value : null;
}

function triStateLabel(value) {
  return value === 1 ? "да" : value === 0 ? "нет" : "неизвестно";
}

function wireTriState(select, f) {
  select.value = f.value === null ? "unknown" : String(f.value);
  select.onchange = () => {
    f.value = select.value === "unknown" ? null : Number(select.value);
    updateCounters();
  };
}

function wireEditable(el, f, multilineValue) {
  const readValue = () => multilineValue ? el.innerText : el.textContent;
  const commit = () => {
    f.value = readValue().trim();
    el.textContent = f.value;
  };
  el.onblur = commit;
  el.onkeydown = ev => {
    if (ev.key !== "Enter") return;
    ev.preventDefault();
    commit();
    el.blur();
  };
}

let activeChipDrag = null;

function moveChip(f, moved, target, placeAfter = false) {
  const from = f.chips.indexOf(moved);
  let to = f.chips.indexOf(target);
  if (from < 0 || to < 0 || from === to) return false;
  if (placeAfter) to += 1;
  f.chips.splice(from, 1);
  if (from < to) to -= 1;
  f.chips.splice(to, 0, moved);
  return f.chips.indexOf(moved) !== from;
}

function chipPositions(container) {
  return new Map([...container.querySelectorAll(".chip")].map(node =>
    [node, node.getBoundingClientRect()]));
}

function animateChipShift(container, before) {
  if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
  for (const node of container.querySelectorAll(".chip")) {
    const first = before.get(node);
    if (!first || node.classList.contains("drag-source")) continue;
    const last = node.getBoundingClientRect();
    const dx = first.left - last.left;
    const dy = first.top - last.top;
    if (!dx && !dy) continue;
    node.getAnimations?.().forEach(animation => animation.cancel());
    node.animate?.(
      [{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "translate(0, 0)" }],
      { duration: 155, easing: "cubic-bezier(.2,.8,.2,1)" }
    );
  }
}

function nearestChip(container, source, x, y) {
  let best = null;
  let bestDistance = Infinity;
  for (const node of container.querySelectorAll(".chip")) {
    if (node === source) continue;
    const rect = node.getBoundingClientRect();
    const dx = x - (rect.left + rect.width / 2);
    const dy = y - (rect.top + rect.height / 2);
    const distance = dx * dx + dy * dy;
    if (distance < bestDistance) { best = node; bestDistance = distance; }
  }
  return best;
}

function positionDragGhost(state, x, y) {
  state.ghost.style.left = `${x - state.offsetX}px`;
  state.ghost.style.top = `${y - state.offsetY}px`;
}

function syncChipDomOrder(container, chips) {
  const nodes = new Map([...container.querySelectorAll(".chip")].map(node =>
    [node._chipData, node]));
  const anchor = container.querySelector(".add-chip, .add-chip-input");
  for (const chip of chips) {
    const node = nodes.get(chip);
    if (!node) continue;
    anchor ? container.insertBefore(node, anchor) : container.appendChild(node);
  }
}

function cancelChipDragOnEscape(event) {
  if (event.key !== "Escape" || !activeChipDrag) return;
  event.preventDefault();
  finishChipDrag(null, true);
}

function startChipDrag(event, f, ch, el, handle) {
  if (event.button !== 0 || activeChipDrag) return;
  event.preventDefault(); event.stopPropagation();
  const rect = el.getBoundingClientRect();
  const ghost = el.cloneNode(true);
  ghost.classList.add("drag-ghost");
  ghost.classList.remove("drag-source", "drop-target");
  ghost.style.width = `${rect.width}px`;
  ghost.style.height = `${rect.height}px`;
  document.body.appendChild(ghost);
  activeChipDrag = {
    pointerId: event.pointerId, field: f, chip: ch, source: el,
    handle, container: el.parentElement, ghost,
    offsetX: event.clientX - rect.left, offsetY: event.clientY - rect.top,
    originalIndex: f.chips.indexOf(ch), target: null,
  };
  el.classList.add("drag-source");
  positionDragGhost(activeChipDrag, event.clientX, event.clientY);
  handle.setPointerCapture?.(event.pointerId);
  window.addEventListener("pointermove", moveChipDrag, { passive: false });
  window.addEventListener("pointerup", finishChipDrag);
  window.addEventListener("pointercancel", finishChipDrag);
  window.addEventListener("keydown", cancelChipDragOnEscape);
}

function moveChipDrag(event) {
  const state = activeChipDrag;
  if (!state || event.pointerId !== state.pointerId) return;
  event.preventDefault();
  positionDragGhost(state, event.clientX, event.clientY);
  const target = nearestChip(state.container, state.source, event.clientX, event.clientY);
  if (!target || !target._chipData) return;
  if (state.target && state.target !== target) state.target.classList.remove("drop-target");
  state.target = target;
  target.classList.add("drop-target");
  const rect = target.getBoundingClientRect();
  const sameRow = event.clientY >= rect.top && event.clientY <= rect.bottom;
  const placeAfter = sameRow
    ? event.clientX > rect.left + rect.width / 2
    : event.clientY > rect.top + rect.height / 2;
  const before = chipPositions(state.container);
  if (!moveChip(state.field, state.chip, target._chipData, placeAfter)) return;
  placeAfter ? target.after(state.source) : target.before(state.source);
  animateChipShift(state.container, before);
}

function finishChipDrag(event, cancel = false) {
  const state = activeChipDrag;
  if (!state || (event && event.pointerId !== undefined && event.pointerId !== state.pointerId)) return;
  if (cancel) {
    const current = state.field.chips.indexOf(state.chip);
    if (current >= 0) {
      state.field.chips.splice(current, 1);
      state.field.chips.splice(state.originalIndex, 0, state.chip);
      syncChipDomOrder(state.container, state.field.chips);
    }
  }
  state.target?.classList.remove("drop-target");
  state.source.classList.remove("drag-source");
  if (state.handle.hasPointerCapture?.(state.pointerId))
    state.handle.releasePointerCapture(state.pointerId);
  state.ghost.remove();
  activeChipDrag = null;
  window.removeEventListener("pointermove", moveChipDrag);
  window.removeEventListener("pointerup", finishChipDrag);
  window.removeEventListener("pointercancel", finishChipDrag);
  window.removeEventListener("keydown", cancelChipDragOnEscape);
}

function makeChip(f, ch) {
  const el = document.createElement("span");
  el.className = "chip" + (ch.isNew ? " new" : "") + (ch.on ? "" : " offc");
  el.title = "клик — включить/исключить · двойной клик — исправить";
  if (COMMA_FIELDS.has(f.name)) {
    const handle = document.createElement("button");
    handle.type = "button";
    handle.className = "drag-handle";
    handle.textContent = "⋮⋮";
    handle.title = "перетащить перевод · Alt+←/→ — сдвинуть";
    handle.setAttribute("aria-label", `Переместить «${ch.text}» в поле ${f.name}`);
    handle.draggable = false;
    handle.onclick = handle.ondblclick = e => e.stopPropagation();
    handle.onpointerdown = e => startChipDrag(e, f, ch, el, handle);
    handle.onkeydown = e => {
      if (!e.altKey || !["ArrowLeft", "ArrowRight"].includes(e.key)) return;
      e.preventDefault(); e.stopPropagation();
      const from = f.chips.indexOf(ch);
      const to = e.key === "ArrowLeft" ? from - 1 : from + 1;
      if (to < 0 || to >= f.chips.length) return;
      const target = f.chips[to];
      const targetNode = [...el.parentElement.querySelectorAll(".chip")]
        .find(node => node._chipData === target);
      if (!targetNode) return;
      const before = chipPositions(el.parentElement);
      if (!moveChip(f, ch, target, e.key === "ArrowRight")) return;
      e.key === "ArrowRight" ? targetNode.after(el) : targetNode.before(el);
      animateChipShift(el.parentElement, before);
    };
    el.appendChild(handle);
  }
  el._chipData = ch;
  const label = document.createElement("span");
  label.textContent = ch.text;
  el.appendChild(label);
  // Single click toggles AFTER a short delay so it can be cancelled by dblclick.
  let clickTimer = null;
  let editing = false;
  el.onclick = e => {
    if (e.target.closest(".drag-handle")) return;
    if (editing || clickTimer) return;
    clickTimer = setTimeout(() => {
      clickTimer = null;
      setChipEnabled(ch, el, !ch.on);
    }, 250);
  };
  el.ondblclick = e => {
    if (e.target.closest(".drag-handle")) return;
    e.stopPropagation();
    if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; }
    if (editing) return;
    editing = true;
    const inp = document.createElement("input");
    inp.value = ch.text;
    inp.size = Math.max(8, ch.text.length + 2);
    label.replaceWith(inp); inp.focus(); inp.select();
    const finishEdit = cancel => {
      if (!editing) return;
      const nextText = cancel ? ch.text : (inp.value.trim() || ch.text);
      editing = false;
      inp.onblur = null;
      ch.text = nextText;
      label.textContent = ch.text;
      inp.replaceWith(label);
    };
    inp.onblur = () => finishEdit(false);
    inp.onkeydown = ev => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        finishEdit(false);
      }
      if (ev.key === "Escape") {
        ev.preventDefault();
        finishEdit(true);
      }
    };
  };
  return el;
}

function makeAddChip(f) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "add-chip";
  btn.textContent = "+";
  btn.title = "добавить перевод";
  btn.setAttribute("aria-label", `Добавить значение в поле ${f.name}`);
  btn.onclick = () => {
    const inp = document.createElement("input");
    inp.type = "text";
    inp.className = "add-chip-input";
    inp.placeholder = "новый перевод";
    inp.setAttribute("aria-label", `Новое значение поля ${f.name}`);
    btn.replaceWith(inp);
    inp.focus();

    let finished = false;
    const done = (save = true) => {
      if (finished) return;
      finished = true;
      const text = inp.value.trim();
      if (save && text) {
        const chip = { text, on: true, isNew: true };
        f.chips.push(chip);
        inp.replaceWith(makeChip(f, chip), makeAddChip(f));
      } else {
        inp.replaceWith(makeAddChip(f));
      }
    };
    inp.onblur = () => done(true);
    inp.onkeydown = ev => {
      if (ev.key === "Enter") { ev.preventDefault(); done(true); }
      if (ev.key === "Escape") { ev.preventDefault(); done(false); }
    };
  };
  return btn;
}

function updateCounters() {
  const diffWords = model.filter(m => !m.isPending);
  const pending = model.filter(m => m.isPending);
  const accepted = diffWords.filter(m => m.accepted).length;
  document.getElementById("cWords").innerHTML =
    `слов: <b>${model.length}</b> · с изменениями: <b>${diffWords.length}</b> · в apply: <b>${accepted}</b>`;
  document.getElementById("cFields").innerHTML = `полей-диффов: <b>${shownFields}</b>`;
  document.getElementById("cPending").innerHTML = `не обработано: <b class="warn">${pending.length}</b>`;
}

// ---- Export: rebuild an apply-compatible JSON from the current choices ----
function buildApply() {
  const words = [];
  for (const m of model) {
    if (!m.accepted || m.isPending) continue;
    const card = JSON.parse(JSON.stringify(m.word));
    for (const f of m.fields) {
      if (!f.accepted) { card[f.name] = f.current; continue; }  // rejected -> keep DB value
      card[f.name] = f.kind === "chips"
        ? f.chips.filter(c => c.on).map(c => c.text).join(NL_FIELDS.has(f.name) ? "\n" : ", ")
        : f.value;
    }
    words.push(card);
  }
  return JSON.stringify({ job_id: DATA.job_id, topic: DATA.topic,
    generated_at: DATA.generated_at, edited_in: "html-review",
    words }, null, 2);
}

function downloadJson() {
  const blob = new Blob([buildApply()], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `vf_review_job${DATA.job_id}_apply.json`;
  a.click(); URL.revokeObjectURL(a.href);
}

async function copyJson() {
  try { await navigator.clipboard.writeText(buildApply()); }
  catch {
    const ta = document.createElement("textarea");
    ta.value = buildApply(); document.body.appendChild(ta);
    ta.select(); document.execCommand("copy"); ta.remove();
  }
  const t = document.getElementById("toast");
  t.style.display = "block"; setTimeout(() => t.style.display = "none", 1500);
}

// ---- Init ----
(function init() {
  document.title = `VF review — job ${DATA.job_id}`;
  document.getElementById("title").textContent = `🌐 VF review — job ${DATA.job_id}`;
  const types = [...new Set(model.map(m => m.word.type).filter(Boolean))];
  const fT = document.getElementById("fType");
  types.forEach(t => fT.append(new Option("тип: " + t, t)));
  const fields = [...new Set(model.flatMap(m => m.fields.map(f => f.name)))];
  const fF = document.getElementById("fField");
  fields.forEach(f => fF.append(new Option("поле: " + f, f)));
  fT.onchange = fF.onchange = render;
  document.getElementById("fSearch").oninput = render;
})();

render();
</script>
</body>
</html>
"""


def render_review_html(doc):
    """Self-contained review page for `doc` (a build_review_document dict).

    The JSON payload is embedded into a <script> block, so any literal "</"
    inside values must be escaped to keep the parser from closing the script
    tag early ("</script>" in an example sentence would truncate the page).
    """
    payload = json.dumps(doc, ensure_ascii=False).replace("</", "<\\/")
    return _TEMPLATE.replace(_DATA_PLACEHOLDER, payload)


def review_html_filename(job_id):
    return f"vf_review_job{job_id}.html"
