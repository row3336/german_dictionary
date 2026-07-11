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
    display: inline-block; margin: 2px 4px 2px 0; padding: 3px 10px;
    border-radius: 999px; border: 1px solid var(--border); background: #2b323b;
    cursor: pointer; user-select: none; font-size: 13px;
  }
  .chip.new { background: var(--green-bg); border-color: var(--green); color: #7ee787; }
  .chip.offc { background: transparent; border-style: dashed; color: var(--muted);
               text-decoration: line-through; }
  .chip input { background: var(--panel2); color: var(--text); border: 1px solid var(--accent);
                border-radius: 6px; font-size: 13px; padding: 1px 6px; }
  .single {
    display: inline-block; min-width: 60px; padding: 3px 10px; border-radius: 6px;
    border: 1px solid var(--green); background: var(--green-bg); color: #7ee787; cursor: text;
  }
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
<div class="sub">Клик по чипу — включить/выключить значение · двойной клик — исправить текст · галка справа — принять/отклонить поле · галка в шапке слова — слово целиком. Закончив, жми «Скачать apply-JSON» и отправь файл боту.</div>

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
// Field order used to synthesize a diff for ADD jobs (new cards have no diff).
const CARD_FIELDS = ["type", "article", "german", "plural", "infinitive", "prateritum",
  "partizip2", "komparativ", "superlativ", "russian", "ukrainian", "english",
  "example", "cefr_level"];

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
    for (const f of CARD_FIELDS)
      if (String(w[f] || "").trim()) diff[f] = { current: "", vf: String(w[f]) };
  }
  const isPending = r.status !== "done" || !Object.keys(diff).length;
  const fields = Object.entries(diff).map(([fname, d]) => {
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

function render() {
  const type = document.getElementById("fType").value;
  const field = document.getElementById("fField").value;
  const q = norm(document.getElementById("fSearch").value || "");
  const list = document.getElementById("list");
  const pend = document.getElementById("pending");
  list.innerHTML = ""; pend.innerHTML = "";
  let shownFields = 0;

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
    lab.querySelector("input").onchange = e => { m.accepted = e.target.checked; render(); };
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
        tdCur.appendChild(multiline(f.current || "—"));

        const tdNew = document.createElement("td"); tdNew.className = "new";
        tdNew.innerHTML = `<span class="lbl">станет</span>`;
        if (f.kind === "chips") {
          f.chips.forEach(ch => tdNew.appendChild(makeChip(f, ch)));
        } else {
          const sp = document.createElement("span");
          sp.className = "single"; sp.contentEditable = "true"; sp.textContent = f.value;
          sp.onblur = () => { f.value = sp.textContent.trim(); };
          tdNew.appendChild(sp);
        }

        const tdC = document.createElement("td"); tdC.className = "fcheck";
        const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = f.accepted;
        cb.onchange = () => { f.accepted = cb.checked; render(); };
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
  updateCounters(shownFields);
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

function makeChip(f, ch) {
  const el = document.createElement("span");
  el.className = "chip" + (ch.isNew ? " new" : "") + (ch.on ? "" : " offc");
  el.textContent = ch.text;
  el.title = "клик — включить/исключить · двойной клик — исправить";
  // Single click toggles AFTER a short delay so it can be cancelled by dblclick
  // (an immediate toggle re-renders the DOM and the second click never lands).
  let clickTimer = null;
  let editing = false;
  el.onclick = () => {
    if (editing || clickTimer) return;
    clickTimer = setTimeout(() => {
      clickTimer = null;
      ch.on = !ch.on;
      render();
    }, 250);
  };
  el.ondblclick = e => {
    e.stopPropagation();
    if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; }
    if (editing) return;
    editing = true;
    const inp = document.createElement("input");
    inp.value = ch.text;
    inp.size = Math.max(8, ch.text.length + 2);
    el.textContent = ""; el.appendChild(inp); inp.focus(); inp.select();
    const done = () => { ch.text = inp.value.trim() || ch.text; render(); };
    inp.onblur = done;
    inp.onkeydown = ev => {
      if (ev.key === "Enter") inp.blur();
      if (ev.key === "Escape") { inp.value = ch.text; inp.blur(); }
    };
  };
  return el;
}

function updateCounters(shownFields) {
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
