/* Mini App shell + main screen + dictionary + error states (miniapp.md §8).
   Vanilla JS, no build step (§8.1). The frontend renders whatever `strings` /me
   sends and hardcodes no human-readable text of its own - every label below is
   `S.some_key`, resolved once /me has answered.

   The quiz screens live next door in quiz.js, which is a separate file for the
   same reason the backend splits its flows: the quiz cycle is a state machine
   with its own rules (a session, a one-shot token, an input that must never lose
   focus), and none of that belongs in the shell. What the two share is exported
   through `window.App` at the bottom of this file - the shell's building blocks,
   nothing of the shell's own state. */

(() => {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;

  // ---------------------------------------------------------------------
  // Telegram bootstrap: theme, viewport height, safe area, back button.
  // ---------------------------------------------------------------------

  function applyTheme() {
    const scheme = (tg && tg.colorScheme) || "light";
    document.documentElement.dataset.theme = scheme;
    const p = (tg && tg.themeParams) || {};
    const root = document.documentElement.style;
    const map = {
      "--bg": p.bg_color,
      "--text": p.text_color,
      "--hint": p.hint_color,
      "--secondary-bg": p.secondary_bg_color || p.bg_color,
      "--separator": p.section_separator_color,
    };
    for (const [name, value] of Object.entries(map)) {
      if (value) root.setProperty(name, value);
    }
  }

  function applyViewport() {
    const h = (tg && tg.viewportStableHeight) || window.innerHeight;
    document.documentElement.style.setProperty("--app-height", h + "px");
  }

  function initTelegram() {
    if (!tg) return;
    tg.ready();
    tg.expand();
    applyTheme();
    applyViewport();
    tg.onEvent("themeChanged", applyTheme);
    tg.onEvent("viewportChanged", applyViewport);
  }

  // ---------------------------------------------------------------------
  // API client
  // ---------------------------------------------------------------------

  const API_ROOT = "/api/v1/";

  class ApiError extends Error {
    constructor(status, code) {
      super(code || String(status));
      this.status = status;
      this.code = code;
    }
  }

  async function api(path, opts) {
    const initData = (tg && tg.initData) || "";
    let resp;
    try {
      resp = await fetch(API_ROOT + path, {
        method: (opts && opts.method) || "GET",
        headers: {
          Authorization: "tma " + initData,
          ...((opts && opts.body) ? { "Content-Type": "application/json" } : {}),
        },
        body: opts && opts.body ? JSON.stringify(opts.body) : undefined,
      });
    } catch (err) {
      throw new ApiError(0, "network_error");
    }
    let payload = {};
    try {
      payload = await resp.json();
    } catch (err) {
      /* empty or non-JSON body - the status code still carries the outcome */
    }
    if (!resp.ok) {
      throw new ApiError(resp.status, payload.error);
    }
    return payload;
  }

  // ---------------------------------------------------------------------
  // App state
  // ---------------------------------------------------------------------

  const state = {
    me: null, // /me payload, including `strings` (S below)
    tab: "quizzes", // "quizzes" | "dictionary"
  };

  // Stack of zero-arg render thunks. goTo(fn) pushes the screen we are leaving so
  // the Telegram BackButton can return to it; goTo(fn, {root: true}) starts a new
  // stack (tab switches, and any screen with no meaningful "back").
  let navStack = [];
  let currentScreen = null;

  let S = {}; // shorthand for state.me.strings, refreshed on each /me load

  function t(key) {
    return (S && S[key]) || key;
  }

  // ---------------------------------------------------------------------
  // Chrome: root container, toast, back button
  // ---------------------------------------------------------------------

  const root = document.getElementById("app");
  const toastEl = document.getElementById("toast");
  let toastTimer = null;

  function toast(message) {
    toastEl.textContent = message;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove("show"), 2200);
  }

  // A screen that owns something outside the DOM - a live quiz session - leaves it
  // here, and it runs once, whichever way the screen is left. Without it the
  // BackButton would strand an open session on the server (§4.2).
  let leaveHook = null;

  function setLeaveHook(fn) {
    leaveHook = fn;
  }

  function runLeaveHook() {
    const hook = leaveHook;
    leaveHook = null;
    if (hook) hook();
  }

  // goTo(fn) runs `fn` and remembers the screen we were on, so the Telegram
  // BackButton returns to it. goTo(fn, {root: true}) starts a fresh stack - for
  // tab switches and anything else with no meaningful "back" (§8.2: there is no
  // in-app back arrow, only the system BackButton).
  function goTo(renderFn, opts) {
    runLeaveHook();
    if ((opts && opts.root) || !currentScreen) {
      navStack = [];
    } else {
      navStack.push(currentScreen);
    }
    currentScreen = renderFn;
    renderFn();
    if (tg && tg.BackButton) {
      if (navStack.length) tg.BackButton.show();
      else tg.BackButton.hide();
    }
  }

  function goBack() {
    runLeaveHook();
    const prev = navStack.pop();
    if (prev) {
      currentScreen = prev;
      prev();
      if (tg && tg.BackButton && !navStack.length) tg.BackButton.hide();
    } else {
      goTo(renderMain, { root: true });
    }
  }

  if (tg && tg.BackButton) tg.BackButton.onClick(goBack);

  // ---------------------------------------------------------------------
  // Generic building blocks
  // ---------------------------------------------------------------------

  function el(html) {
    const div = document.createElement("div");
    div.innerHTML = html.trim();
    return div.firstElementChild;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[c]));
  }

  function stateScreen({ icon, title, hint, ctaLabel, onCta, cta2Label, onCta2 }) {
    const wrap = el(`
      <div class="screen"><div class="state">
        <div class="ic">${icon}</div>
        <h4>${escapeHtml(title)}</h4>
        ${hint ? `<p>${escapeHtml(hint)}</p>` : ""}
        ${ctaLabel ? `<button class="cta">${escapeHtml(ctaLabel)}</button>` : ""}
        ${cta2Label ? `<button class="cta2">${escapeHtml(cta2Label)}</button>` : ""}
      </div></div>`);
    if (ctaLabel) wrap.querySelector(".cta").addEventListener("click", onCta);
    if (cta2Label) wrap.querySelector(".cta2").addEventListener("click", onCta2);
    return wrap;
  }

  function mount(node) {
    root.innerHTML = "";
    root.appendChild(node);
  }

  // ---------------------------------------------------------------------
  // Word card component - reused by search results and by word-detail nav
  // (miniapp.md §8.4a; the same markup the quiz feedback will reuse from M4).
  // ---------------------------------------------------------------------

  const GENDER_CLASS = { m: "m", f: "f", n: "n" };

  function cardHeadClass(card) {
    return card.gender && GENDER_CLASS[card.gender] ? " " + GENDER_CLASS[card.gender] : "";
  }

  function renderCardHead(card) {
    const g = card.gender;
    const artpill = card.article
      ? `<span class="artpill">${escapeHtml(card.article)}</span>`
      : "";
    let sub = "";
    if (card.type === "nomen" && card.forms && card.forms.plural) {
      sub = `<div class="csub">pl. die <b>${escapeHtml(card.forms.plural)}</b></div>`;
    } else if (card.type === "verb" && card.forms) {
      const parts = [card.forms.prateritum, card.forms.partizip2].filter(Boolean);
      if (parts.length) {
        sub = `<div class="csub">${parts
          .map((p, i) => (i === parts.length - 1 ? `<b>${escapeHtml(p)}</b>` : escapeHtml(p)))
          .join(" · ")}</div>`;
      }
    } else if (card.type === "adjektiv" && card.forms) {
      const parts = [card.forms.komparativ, card.forms.superlativ].filter(Boolean);
      if (parts.length) {
        sub = `<div class="csub">${parts
          .map((p, i) => (i === parts.length - 1 ? `<b>${escapeHtml(p)}</b>` : escapeHtml(p)))
          .join(" · ")}</div>`;
      }
    }
    const hintBits = [card.preposition_type, card.case_type].filter(Boolean);
    const hint = hintBits.length
      ? `<span class="chint">${hintBits.map(escapeHtml).join(" · ")}</span>`
      : card.type && !["nomen", "verb", "adjektiv"].includes(card.type)
        ? `<span class="chint">${escapeHtml(card.type)}</span>`
        : "";
    return `
      <div class="chead${cardHeadClass(card)}">
        <div class="crow">${artpill}<span class="cword">${escapeHtml(card.german)}</span></div>
        ${sub}
        ${card.translation ? `<div class="ctr">${escapeHtml(card.translation)}</div>` : ""}
        ${hint}
      </div>`;
  }

  function renderConjugation(conj) {
    if (!conj) return "";
    const table = (rows) =>
      rows.map(([p, f]) => `<span class="p">${escapeHtml(p)}</span><span>${escapeHtml(f)}</span>`).join("");
    const col = (label, rows) =>
      rows
        ? `<div class="conj-wrap"><h3 class="conj-h">${escapeHtml(label)}</h3><div class="conj">${table(rows)}</div></div>`
        : "";
    const cols = col("Präsens", conj.praesens) + col("Präteritum", conj.praeteritum);
    return cols ? `<section class="blk"><div class="conj-cols">${cols}</div></section>` : "";
  }

  function renderCardBody(card, opts) {
    opts = opts || {};
    let html = "";
    if (card.synonyms && card.synonyms.length) {
      html += `<section class="blk"><h3>${escapeHtml(t("card_synonyms"))}</h3>${card.synonyms
        .map(
          (s) =>
            `<button class="chip" data-syn-id="${escapeHtml(s.id)}">${
              s.article ? escapeHtml(s.article) + " " : ""
            }${escapeHtml(s.german)}</button>`
        )
        .join("")}</section>`;
    }
    html += renderConjugation(card.conjugation);
    if (card.media && card.media.example) {
      html += `<section class="blk"><h3>${escapeHtml(t("card_example"))}</h3><div class="mine">${escapeHtml(
        card.media.example
      )}</div></section>`;
    }
    if (card.media && card.media.image) {
      html += `<section class="blk"><h3>${escapeHtml(t("card_image"))}</h3><div class="mine">${escapeHtml(
        card.media.image
      )}</div></section>`;
    }
    if (card.study) {
      const rows = [];
      if (card.study.state) {
        const stateLabel = { fsrs: "FSRS", learning: "Learning", queue_learning: "Learning" }[card.study.state] || card.study.state;
        rows.push([t("card_study"), stateLabel]);
      }
      if (card.study.due) rows.push([t("card_study_due"), card.study.due]);
      if (card.study.weak_forms && card.study.weak_forms.length) {
        rows.push([t("card_study_weak"), card.study.weak_forms.join(", ")]);
      }
      if (rows.length) {
        html += `<div class="study">${rows
          .filter((r) => r[1])
          .map((r) => `<div class="r"><span>${escapeHtml(r[0])}</span><b>${escapeHtml(r[1])}</b></div>`)
          .join("")}</div>`;
      }
    }
    return html;
  }

  function renderWordCard(card, opts) {
    opts = opts || {};
    const wrap = el(`
      <div class="scroll">
        ${opts.synonymOf ? `<div class="synnote">${escapeHtml(t("card_synonym_of"))} <b>${escapeHtml(opts.synonymOf)}</b></div>` : ""}
        ${renderCardHead(card)}
        ${renderCardBody(card)}
      </div>`);
    wrap.querySelectorAll("[data-syn-id]").forEach((btn) => {
      btn.addEventListener("click", () => openWordDetail(btn.dataset.synId, card.german));
    });
    return wrap;
  }

  function openWordDetail(wordId, synonymOfLabel) {
    goTo(() => renderWordDetailScreen(wordId, synonymOfLabel));
  }

  async function renderWordDetailScreen(wordId, synonymOfLabel) {
    const screen = el('<div class="screen"></div>');
    mount(screen);
    screen.appendChild(el('<div class="skeleton"><div class="sk-tile" style="height:180px"></div></div>'));
    let card;
    try {
      ({ card } = await api("word/" + encodeURIComponent(wordId)));
    } catch (err) {
      handleApiError(err);
      return;
    }
    screen.innerHTML = "";
    screen.appendChild(renderWordCard(card, { synonymOf: synonymOfLabel }));
  }

  // ---------------------------------------------------------------------
  // Dictionary tab (miniapp.md §7.5, §8.5)
  // ---------------------------------------------------------------------

  let searchDebounce = null;

  function renderDictionary() {
    const screen = el(`
      <div class="screen">
        <div class="searchbar"><div class="field">
          <span class="mag">🔍</span>
          <input type="text" autocomplete="off" autocorrect="off" autocapitalize="off"
                 spellcheck="false" placeholder="${escapeHtml(t("search_placeholder"))}">
        </div></div>
        <div class="scroll" id="dict-results">
          <div class="empty"><span class="ic">🔎</span><p>${escapeHtml(t("search_placeholder"))}</p></div>
        </div>
        ${tabBarHtml("dictionary")}
      </div>`);
    mount(screen);
    wireTabBar(screen);

    const input = screen.querySelector("input");
    const results = screen.querySelector("#dict-results");
    input.addEventListener("input", () => {
      clearTimeout(searchDebounce);
      const q = input.value;
      if (!q.trim()) {
        results.innerHTML = `<div class="empty"><span class="ic">🔎</span><p>${escapeHtml(t("search_placeholder"))}</p></div>`;
        return;
      }
      searchDebounce = setTimeout(() => runSearch(q, results), 250);
    });
    input.focus();
  }

  async function runSearch(query, results) {
    let data;
    try {
      data = await api("search?q=" + encodeURIComponent(query));
    } catch (err) {
      handleApiError(err);
      return;
    }
    if (data.result === "empty") {
      results.innerHTML = `<div class="empty"><span class="ic">🔎</span><p>${escapeHtml(t("search_empty"))}</p></div>`;
    } else if (data.result === "card") {
      results.innerHTML = "";
      results.appendChild(renderWordCard(data.card));
    } else if (data.result === "choice") {
      results.innerHTML = data.candidates.map(candidateRowHtml).join("");
      results.querySelectorAll("[data-word-id]").forEach((rowEl) => {
        rowEl.addEventListener("click", () => openWordDetail(rowEl.dataset.wordId));
      });
    }
  }

  function candidateRowHtml(c) {
    const genderCls = c.gender && GENDER_CLASS[c.gender] ? " " + GENDER_CLASS[c.gender] : "";
    const headword = c.article ? `${escapeHtml(c.article)} ${escapeHtml(c.german)}` : escapeHtml(c.german);
    return `
      <button class="row${genderCls}" data-word-id="${escapeHtml(c.id)}">
        <div class="dot"></div>
        <div class="body">
          <div class="de">${headword}${c.translation ? ` · <span class="tr">${escapeHtml(c.translation)}</span>` : ""}</div>
          <div class="ty">${escapeHtml(c.type || "")}</div>
        </div>
      </button>`;
  }

  // ---------------------------------------------------------------------
  // Main screen (miniapp.md §7.1, §8.5): mode tiles from /me due counts.
  // ---------------------------------------------------------------------

  const FSRS_TILES = [
    ["l1_de", "mode_l1_de"],
    ["de_l1", "mode_de_l1"],
    ["pl_sg", "mode_pl_sg"],
  ];
  const WEAK_TILES = [
    ["weak_article", "mode_weak_article"],
    ["weak_plural", "mode_weak_plural"],
    ["weak_prateritum", "mode_weak_prateritum"],
    ["weak_partizip2", "mode_weak_partizip2"],
  ];

  // Every mode's own label key, for the tiles here and for the quiz header.
  const MODE_LABEL = Object.fromEntries(
    FSRS_TILES.concat(WEAK_TILES, [["learning", "mode_learning"]])
  );

  function tileHtml(mode, labelKey, due) {
    const shown = due > 0 ? `<div class="n">${due}</div>` : `<div class="n zero">${escapeHtml(t("due_none"))}</div>`;
    return `<div class="tile" data-mode="${mode}" data-due="${due}" role="button" tabindex="0">${shown}<div class="l">${escapeHtml(t(labelKey))}</div></div>`;
  }

  function topicsChipHtml(me) {
    const value = me.topic_filter === "all" ? t("topics_all") : me.topic_filter;
    return `<div class="chipbar"><span class="f">${escapeHtml(t("topics_label"))}: ${escapeHtml(value)} · ${escapeHtml(
      t("topics_bot_only")
    )}</span></div>`;
  }

  function tabBarHtml(active) {
    return `
      <div class="tabs">
        <button data-tab="quizzes" class="${active === "quizzes" ? "on" : ""}"><span class="ic">◉</span>${escapeHtml(t("tab_quizzes"))}</button>
        <button data-tab="dictionary" class="${active === "dictionary" ? "on" : ""}"><span class="ic">🔍</span>${escapeHtml(t("tab_dictionary"))}</button>
      </div>`;
  }

  function wireTabBar(screen) {
    screen.querySelectorAll(".tabs button").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.dataset.tab === state.tab) return;
        state.tab = btn.dataset.tab;
        goTo(state.tab === "dictionary" ? renderDictionary : renderMain, { root: true });
      });
    });
  }

  function renderMain() {
    const me = state.me;
    const due = me.due || {};
    const tiles = FSRS_TILES.map(([mode, label]) => tileHtml(mode, label, due[mode] || 0)).join("") +
      WEAK_TILES.map(([mode, label]) => tileHtml(mode, label, due[mode] || 0)).join("") +
      `<div class="tile wide" data-mode="learning" data-due="${me.learning.active}" role="button" tabindex="0">
         <div class="n${me.learning.active ? "" : " zero"}">${me.learning.active || t("due_none")}</div>
         <div class="l">${escapeHtml(t("mode_learning"))}</div>
         <button class="tile-gear" data-learning-settings type="button" aria-label="settings">⚙</button>
       </div>`;
    const screen = el(`
      <div class="screen">
        ${topicsChipHtml(me)}
        <div class="scroll"><div class="tiles">${tiles}</div></div>
        ${tabBarHtml("quizzes")}
      </div>`);
    mount(screen);
    wireTabBar(screen);
    const gear = screen.querySelector("[data-learning-settings]");
    if (gear) {
      gear.addEventListener("click", (ev) => {
        ev.stopPropagation();
        goTo(renderLearningSettings);
      });
    }
    screen.querySelectorAll(".tile[data-mode]").forEach((tile) => {
      tile.addEventListener("click", () => openMode(tile.dataset.mode, Number(tile.dataset.due || 0)));
    });
  }

  // An empty tile does not open a session. Starting one takes the quiz away from
  // the chat (§4.2 п.1), and doing that only to be told "nothing due" would end a
  // conversation the user is in the middle of.
  function openMode(mode, due) {
    if (due > 0) {
      window.App.quiz.start(mode);
    } else if (mode === "learning") {
      // An empty batch is not "nothing to repeat": it is a batch waiting to be
      // filled, and that screen offers the top-up instead of a dead end.
      goTo(() => window.App.quiz.renderLearningEmpty({}));
    } else {
      goTo(renderNoDue);
    }
  }

  function renderNoDue() {
    const screen = stateScreen({
      icon: "✅",
      title: t("state_no_due"),
      hint: t("state_no_due_hint"),
      ctaLabel: t("back_to_menu"),
      onCta: () => goBack(),
    });
    mount(screen);
  }

  // ---------------------------------------------------------------------
  // Learning settings (miniapp.md §7.6a): the one algorithm screen the app
  // writes, worded in plain language instead of the bot's storage-key labels.
  // ---------------------------------------------------------------------

  const LS_FIELDS = [
    ["daily_qty", "learning_settings_daily_qty_label", "learning_settings_daily_qty_hint", ""],
    ["quorum_pct", "learning_settings_quorum_label", "learning_settings_quorum_hint", "%"],
    ["reminder_delay_minutes", "learning_settings_reminder_delay_label", "learning_settings_reminder_delay_hint", "unit_minutes"],
    ["reminder_cooldown_minutes", "learning_settings_reminder_cooldown_label", "learning_settings_reminder_cooldown_hint", "unit_minutes"],
  ];

  function learningSettingsFormHtml(data) {
    const rows = LS_FIELDS.map(([field, labelKey, hintKey, unit]) => {
      const suffix = unit === "unit_minutes" ? t("unit_minutes") : unit;
      return `
        <section class="blk">
          <h3>${escapeHtml(t(labelKey))}</h3>
          <p class="hint">${escapeHtml(t(hintKey))}</p>
          <div class="field"><input type="number" inputmode="decimal" data-field="${field}" value="${escapeHtml(data[field])}">${
            suffix ? `<span class="unit">${escapeHtml(suffix)}</span>` : ""
          }</div>
        </section>`;
    }).join("");
    const intervals = `
      <section class="blk">
        <h3>${escapeHtml(t("learning_settings_intervals_label"))}</h3>
        <p class="hint">${escapeHtml(t("learning_settings_intervals_hint"))}</p>
        <div class="field"><input type="text" inputmode="decimal" data-field="intervals_minutes"
               value="${escapeHtml(data.intervals_minutes.join(", "))}"><span class="unit">${escapeHtml(
                 t("unit_minutes")
               )}</span></div>
      </section>`;
    return (
      rows +
      intervals +
      `<div class="stack"><div class="bigbtn" data-save>${escapeHtml(t("learning_settings_save"))}</div></div>`
    );
  }

  function wireLearningSettingsForm(body) {
    const saveBtn = body.querySelector("[data-save]");
    saveBtn.addEventListener("click", async () => {
      const payload = {};
      for (const [field] of LS_FIELDS) {
        const input = body.querySelector(`[data-field="${field}"]`);
        const num = parseFloat(input.value);
        if (!Number.isNaN(num)) payload[field] = num;
      }
      const intervalsInput = body.querySelector('[data-field="intervals_minutes"]');
      const list = intervalsInput.value
        .split(",")
        .map((s) => parseFloat(s.trim()))
        .filter((n) => !Number.isNaN(n));
      if (list.length) payload.intervals_minutes = list;

      let data;
      try {
        data = await api("learning/settings", { method: "POST", body: payload });
      } catch (err) {
        if (err instanceof ApiError && err.status === 400) {
          toast(t("learning_settings_invalid"));
        } else {
          handleApiError(err);
        }
        return;
      }
      body.innerHTML = learningSettingsFormHtml(data);
      wireLearningSettingsForm(body);
      toast(t("learning_settings_saved"));
    });
  }

  async function renderLearningSettings() {
    const screen = el(`
      <div class="screen">
        <div class="scroll" id="ls-body">
          <div class="thead"><h4>${escapeHtml(t("learning_settings_title"))}</h4></div>
          <div class="skeleton"><div class="sk-tile" style="height:220px"></div></div>
        </div>
      </div>`);
    mount(screen);
    const body = screen.querySelector("#ls-body");
    let data;
    try {
      data = await api("learning/settings");
    } catch (err) {
      handleApiError(err);
      return;
    }
    body.innerHTML =
      `<div class="thead"><h4>${escapeHtml(t("learning_settings_title"))}</h4></div>` + learningSettingsFormHtml(data);
    wireLearningSettingsForm(body);
  }

  // Reachable once M4 wires a real quiz session into a tile; the 409 contract
  // (§4.2 п.3) already exists server-side, so the screen is built ahead of it.
  function renderSessionTakenOver() {
    const screen = stateScreen({
      icon: "💬",
      title: t("state_taken_over"),
      hint: t("state_taken_over_hint"),
      ctaLabel: t("state_continue_here"),
      onCta: () => goTo(renderMain, { root: true }),
    });
    mount(screen);
  }

  // ---------------------------------------------------------------------
  // Error / access states
  // ---------------------------------------------------------------------

  function openChatFallback() {
    if (tg && tg.close) tg.close();
  }

  function renderNoAccess(code) {
    const bodyKey = { access_pending: "access_pending", access_rejected: "access_rejected", access_unknown: "access_unknown" }[code] || "access_unknown";
    const screen = stateScreen({
      icon: code === "access_pending" ? "⏳" : code === "access_rejected" ? "🚫" : "👋",
      title: t("state_no_access_title"),
      hint: t(bodyKey),
      ctaLabel: t("state_open_chat"),
      onCta: openChatFallback,
    });
    mount(screen);
  }

  function renderOffline(retry) {
    const screen = stateScreen({
      icon: "📡",
      title: t("state_offline"),
      hint: t("state_offline_hint"),
      ctaLabel: t("state_retry"),
      onCta: retry,
    });
    mount(screen);
  }

  function renderGenericError(retry) {
    const screen = stateScreen({
      icon: "⚠️",
      title: t("error_generic"),
      hint: t("error_reopen"),
      ctaLabel: t("state_retry"),
      onCta: retry,
    });
    mount(screen);
  }

  function handleApiError(err) {
    if (!(err instanceof ApiError)) {
      goTo(() => renderGenericError(boot), { root: true });
      return;
    }
    if (err.status === 0) {
      goTo(() => renderOffline(boot), { root: true });
    } else if (err.status === 429) {
      toast(t("error_rate_limited"));
    } else if (err.status === 409 && err.code === "session_taken_over") {
      goTo(renderSessionTakenOver, { root: true });
    } else if (err.status === 401) {
      goTo(() => renderGenericError(boot), { root: true });
    } else if (err.status === 403) {
      goTo(() => renderNoAccess(err.code), { root: true });
    } else {
      goTo(() => renderGenericError(boot), { root: true });
    }
  }

  // ---------------------------------------------------------------------
  // Shared core, for quiz.js (§8.1: separate files, no module bundler)
  // ---------------------------------------------------------------------

  // Only what a second screen file genuinely needs: the API client, navigation,
  // the string table and the card component. `quiz` is filled in by quiz.js when
  // it loads, and is read at click time rather than at wiring time so the load
  // order of the two <script> tags cannot matter.
  window.App = {
    tg,
    state,
    api,
    ApiError,
    t,
    el,
    escapeHtml,
    mount,
    toast,
    goTo,
    goBack,
    setLeaveHook,
    stateScreen,
    renderWordCard,
    renderCardHead,
    renderCardBody,
    renderMain,
    renderNoDue,
    handleApiError,
    GENDER_CLASS,
    MODE_LABEL,
    quiz: null,
  };

  // ---------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------

  async function boot() {
    mount(el(`<div class="skeleton"><div class="sk-tiles">
      <div class="sk-tile"></div><div class="sk-tile"></div>
      <div class="sk-tile"></div><div class="sk-tile"></div>
    </div></div>`));
    let me;
    try {
      me = await api("me");
    } catch (err) {
      handleApiError(err);
      return;
    }
    state.me = me;
    S = me.strings || {};
    document.title = t("app_title");
    goTo(renderMain, { root: true });
  }

  initTelegram();
  boot();
})();
