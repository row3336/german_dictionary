/* Topic filter: the one setting the app writes (miniapp.md §7.6, mockup «Фильтр тем»).

   A third screen file next to app.js and quiz.js, for the same reason quiz.js is
   one: this screen owns a draft. Every tap edits a local selection and nothing
   reaches the server until «Apply», so the user can pass through states the API
   would refuse - an empty selection above all - and back out of them without a
   round trip and without the chat's filter ever having been half-written.

   What it deliberately does NOT own is the meaning of that selection. The chip
   order, which chips exist at all, what 'all' resolves to and how many words a
   selection covers are the server's answers (`GET /topics`); this file draws
   them. The one number it computes locally is the live word count while the user
   taps, and only because `topic` is a single column, so the counts of the chosen
   chips sum to exactly what the server would count.

   `mode` matters as much as the chips: with the chat switched to CEFR levels the
   stored topics are not what quizzes filter by, and applying here switches the
   branch back (topics_api.py). The screen says so rather than letting the user
   tap into a filter that does nothing. */

(() => {
  "use strict";

  const App = window.App;
  const { api, ApiError, t, el, escapeHtml, mount, toast, goBack, handleApiError } = App;

  // word_filter.py's own names for the two filter branches.
  const MODE_CEFR = "cefr";

  // The 400s POST answers with (topics_api.py). Both are fixed by one GET: the
  // level's topics moved under an open screen.
  const ERR_EMPTY = "empty_topics";
  const ERR_UNKNOWN = "unknown_topics";

  // The draft. `names` keeps the server's canonical order, which is also the order
  // the filter string has to be built in.
  const view = {
    names: [],
    counts: {},
    totalWords: 0,
    mode: "",
    selected: new Set(),
  };

  function isAll() {
    return view.names.length > 0 && view.selected.size === view.names.length;
  }

  function selectedWords() {
    let sum = 0;
    for (const name of view.selected) sum += view.counts[name] || 0;
    return sum;
  }

  // ---------------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------------

  function chipHtml(name) {
    const on = view.selected.has(name) ? " on" : "";
    return `<button class="tchip${on}" data-topic="${escapeHtml(name)}">${escapeHtml(name)} <i>${
      view.counts[name] || 0
    }</i></button>`;
  }

  function allChipHtml() {
    return `<button class="tchip all${isAll() ? " on" : ""}" data-all>${escapeHtml(
      t("topics_all")
    )} <i>${view.totalWords}</i></button>`;
  }

  function summaryText() {
    if (isAll()) return `${t("topics_all")} · ${view.totalWords} ${t("words_count")}`;
    return `${t("topics_sum_selected")}: ${view.selected.size} · ${selectedWords()} ${t("words_count")}`;
  }

  function bodyHtml() {
    if (!view.names.length) {
      // A level whose topics were never assigned - the bot's screen is empty here
      // too, so the app says why instead of showing a bare Apply button.
      return `<div class="empty"><span class="ic">🗂</span><p>${escapeHtml(t("topics_none"))}</p></div>`;
    }
    return `
      <div class="tools">
        <button data-select-all>${escapeHtml(t("topics_select_all"))}</button>
        <button data-clear-all>${escapeHtml(t("topics_clear_all"))}</button>
      </div>
      <div class="tgrid">${allChipHtml()}${view.names.map(chipHtml).join("")}</div>
      <div class="tsum">${escapeHtml(summaryText())}</div>`;
  }

  function headHtml() {
    const note =
      view.mode === MODE_CEFR ? `<p class="warn">${escapeHtml(t("topics_mode_cefr"))}</p>` : "";
    return `
      <div class="thead">
        <h4>${escapeHtml(t("topics_label"))}</h4>
        <p>${escapeHtml(t("topics_hint"))}</p>
        ${note}
      </div>`;
  }

  let screen = null;

  // Only the part that a tap changes is redrawn: the header carries the CEFR note,
  // which no tap can affect, and the Apply button must not be rebuilt under a
  // finger that is already on it.
  function repaintBody() {
    const body = screen.querySelector("#topics-body");
    body.innerHTML = bodyHtml();
    wireBody(body);
  }

  function wireBody(body) {
    body.querySelectorAll("[data-topic]").forEach((chip) => {
      chip.addEventListener("click", () => {
        const name = chip.dataset.topic;
        if (view.selected.has(name)) view.selected.delete(name);
        else view.selected.add(name);
        repaintBody();
      });
    });
    const allChip = body.querySelector("[data-all]");
    if (allChip) {
      // The master chip: it shows whether everything is on, so tapping it means
      // "make that true" - and, when it already is, the only move left is to clear.
      allChip.addEventListener("click", () => {
        if (isAll()) view.selected.clear();
        else view.names.forEach((name) => view.selected.add(name));
        repaintBody();
      });
    }
    const selectAll = body.querySelector("[data-select-all]");
    if (selectAll) {
      selectAll.addEventListener("click", () => {
        view.names.forEach((name) => view.selected.add(name));
        repaintBody();
      });
    }
    const clearAll = body.querySelector("[data-clear-all]");
    if (clearAll) {
      clearAll.addEventListener("click", () => {
        view.selected.clear();
        repaintBody();
      });
    }
  }

  function paint() {
    screen.innerHTML =
      headHtml() +
      `<div class="scroll" id="topics-body">${bodyHtml()}</div>` +
      `<div class="stack"><button class="bigbtn" data-apply>${escapeHtml(t("topics_apply"))}</button></div>`;
    wireBody(screen.querySelector("#topics-body"));
    screen.querySelector("[data-apply]").addEventListener("click", apply);
  }

  // ---------------------------------------------------------------------
  // Load / apply
  // ---------------------------------------------------------------------

  function absorb(data) {
    view.names = data.topics.map((topic) => topic.name);
    view.counts = {};
    view.selected = new Set();
    for (const topic of data.topics) {
      view.counts[topic.name] = topic.count;
      if (topic.selected) view.selected.add(topic.name);
    }
    view.totalWords = data.total_words;
    view.mode = data.mode;
  }

  async function load() {
    let data;
    try {
      data = await api("topics");
    } catch (err) {
      handleApiError(err);
      return false;
    }
    absorb(data);
    return true;
  }

  async function apply() {
    if (!view.names.length) {
      goBack();
      return;
    }
    if (!view.selected.size) {
      // The API refuses an empty filter and it is right to: it would leave every
      // quiz without words. Caught here so the user gets the reason, not a 400.
      toast(t("topics_empty"));
      return;
    }
    const value = isAll() ? "all" : view.names.filter((name) => view.selected.has(name)).join("+");
    try {
      await api("topics", { method: "POST", body: { value } });
    } catch (err) {
      if (err instanceof ApiError && (err.code === ERR_UNKNOWN || err.code === ERR_EMPTY)) {
        // The chat moved the book-level while this screen was open. One GET puts
        // the screen back on the level that now exists.
        toast(t("topics_changed"));
        if (await load()) paint();
        return;
      }
      handleApiError(err);
      return;
    }
    // The filter is a divisor of every number on the main screen - the due counts
    // and the learning batch alike - so the tiles are refetched, not just the chip.
    try {
      await App.refreshMe();
    } catch (err) {
      handleApiError(err);
      return;
    }
    toast(t("topics_saved"));
    goBack();
  }

  async function open() {
    screen = el('<div class="screen"></div>');
    mount(screen);
    screen.appendChild(
      el('<div class="skeleton"><div class="sk-tile" style="height:220px"></div></div>')
    );
    if (!(await load())) return;
    paint();
  }

  App.topics = { open };
})();
