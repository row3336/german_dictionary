/* Quiz screens: the answering cycle, the weak-article buttons, the learning
   preview and the session summary (miniapp.md §8.5, §8.5a, M8 part 2).

   One idea runs through the whole file: **the session's DOM is built once**. The
   header, the progress bar and - in the modes that are typed into - the input
   element live from the first question to the last, and only the body between
   them is replaced. That is what keeps the mobile keyboard open across the whole
   session (§8.5a): an input that is never removed is an input that never loses
   focus, so `Enter → verdict → Enter → next word` runs without the keyboard
   closing once.

   The three shapes an answer can take come from the server, not from a mode list
   here: `answer_kind` is `text`, `article_choice` (three buttons, no keyboard) or
   `none` (a learning preview card, read rather than answered). Everything else -
   which words come back as retries, what counts as correct, when a batch is done -
   is the backend's, and this file only shows what it is told.

   The next question travels with the verdict (`answer.next`), so advancing costs
   no round trip: the tap that dismisses the result renders the word that is
   already in hand. */

(() => {
  "use strict";

  const App = window.App;
  const { api, ApiError, t, el, escapeHtml, mount, toast, goTo, setLeaveHook, handleApiError } = App;
  const tg = App.tg;

  // Answer kinds, as serializers.py names them.
  const KIND_TEXT = "text";
  const KIND_ARTICLE = "article_choice";
  const KIND_NONE = "none";

  const ARTICLES = ["der", "die", "das"];
  const ARTICLE_GENDER = { der: "m", die: "f", das: "n" };

  const LEARNING = "learning";
  const PHASE_PREVIEW = "preview";

  // Why a queue is empty (outcomes.py). `no_words` is a filter problem and
  // `no_due` is a matter of waiting - the app says which, as the chat does.
  const EMPTY_NO_WORDS = "no_words";

  // Why a top-up added nothing (common/learning_core.py).
  const FILL_REASON_KEY = {
    batch_full: "learning_fill_batch_full",
    no_new: "learning_fill_no_new",
  };

  // The running session. `pending` is the next step that came back with the
  // verdict; `footKind` is what the bottom of the screen is currently built for,
  // so a text→text transition can leave the input element alone.
  const q = {
    mode: "",
    question: null,
    pending: null,
    phase: "",
    footKind: "",
    busy: false,
    screen: null,
    input: null,
    countdown: null,
  };

  // ---------------------------------------------------------------------
  // Starting and ending a session
  // ---------------------------------------------------------------------

  async function start(mode) {
    let data;
    try {
      data = await api("session/start", { method: "POST", body: { mode } });
    } catch (err) {
      handleApiError(err);
      return;
    }
    q.mode = mode;
    if (data.question) {
      goTo(() => mountSession(data.question));
      return;
    }
    // The session exists even with nothing to ask - `start` created it - so it is
    // closed before the empty screen goes up, rather than left to time out.
    stopQuietly();
    showEmpty(data);
  }

  function stopQuietly() {
    clearCountdown();
    api("session/stop", { method: "POST", body: {} }).catch(() => {
      /* a session that is already gone needs no closing */
    });
  }

  async function stopSession() {
    setLeaveHook(null); // this is the stop; the hook must not fire a second one
    let data;
    try {
      data = await api("session/stop", { method: "POST", body: {} });
    } catch (err) {
      handleApiError(err);
      return;
    }
    if (data.summary) showSummary(data.summary);
    else toMenu();
  }

  function toMenu() {
    setLeaveHook(null);
    goTo(App.renderMain, { root: true });
  }

  // ---------------------------------------------------------------------
  // The session screen: built once, refilled per question (§8.5a)
  // ---------------------------------------------------------------------

  function mountSession(question) {
    q.pending = null;
    q.footKind = "";
    q.screen = el(`
      <div class="screen">
        <div class="qtop"><span data-qmode></span><span data-qcount></span></div>
        <div class="bar"><i data-qbar></i></div>
        <div class="qbody" data-qbody></div>
        <div data-qfoot></div>
      </div>`);
    mount(q.screen);
    // Leaving by the Telegram BackButton has to close the session too, or the
    // server keeps believing the app owns the queue (§4.2).
    setLeaveHook(stopQuietly);
    // A tap anywhere on the result advances, as an equal alternative to Enter for
    // whoever is not typing (§8.5a). Buttons keep their own meaning.
    q.screen.addEventListener("click", (ev) => {
      if (q.phase !== "result" || ev.target.closest("button")) return;
      focusInput();
      advance();
    });
    showQuestion(question);
  }

  function part(name) {
    return q.screen.querySelector(`[data-${name}]`);
  }

  function setHeader(answered, total) {
    const label = t(App.MODE_LABEL[q.mode] || q.mode);
    const phase = q.question && q.question.phase === PHASE_PREVIEW ? "learning_phase_preview" : "learning_phase_quiz";
    part("qmode").textContent = q.mode === LEARNING ? `${label} · ${t(phase)}` : label;
    part("qcount").textContent = total ? `${answered} / ${total}` : "";
    part("qbar").style.width = total ? `${Math.min(100, Math.round((answered / total) * 100))}%` : "0";
  }

  // The bottom of the screen. Rebuilt only when the shape of the answer changes:
  // question→result inside a typed mode must NOT touch it, because rebuilding it
  // would destroy the focused input and close the keyboard. Article buttons are
  // the exception - they carry the previous verdict in their classes and labels,
  // so each question starts from a fresh row.
  function setFoot(kind, html) {
    const foot = part("qfoot");
    if (kind !== q.footKind || kind === KIND_ARTICLE) {
      foot.innerHTML = html;
      q.footKind = kind;
      q.input = foot.querySelector("input");
      const form = foot.querySelector("form");
      if (form) form.addEventListener("submit", onSubmit);
    }
    wireActs(foot);
  }

  function wireActs(scope) {
    scope.querySelectorAll("[data-act]").forEach((btn) => {
      btn.onclick = (ev) => {
        ev.preventDefault();
        focusInput(); // synchronously, inside the gesture, before any request
        ACTIONS[btn.dataset.act]();
      };
    });
  }

  function focusInput() {
    if (q.input) q.input.focus();
  }

  // ---------------------------------------------------------------------
  // The question
  // ---------------------------------------------------------------------

  function showQuestion(question) {
    q.question = question;
    q.pending = null;
    q.phase = "question";
    const progress = question.progress || {};
    const total = Number(progress.total_due || 0);
    setHeader(Math.min(Number(progress.answered || 0) + 1, total), total);
    part("qbody").innerHTML =
      question.answer_kind === KIND_NONE ? previewHtml(question) : questionHtml(question);
    setFoot(question.answer_kind, footHtml(question.answer_kind));
    if (question.answer_kind === KIND_ARTICLE) wireArticleButtons();
    if (question.answer_kind === KIND_TEXT && q.input) {
      q.input.value = "";
      focusInput();
    }
    questionChrome();
  }

  // Back from a verdict to a question: the "tap for the next one" line goes away
  // and the action row returns to the one a question offers.
  function questionChrome() {
    const hint = part("qtap");
    if (hint) hint.hidden = true;
    if (q.footKind === KIND_TEXT) setActs(questionActs());
  }

  function questionHtml(question) {
    const hint = question.hint || {};
    const forms = question.answer_forms || {};
    // De→L1 is the one mode whose "what to type" hint is an interface word rather
    // than a German form name, so it is not in `answer_forms` (§8.5a-bis).
    const main = forms.main || (question.mode === "de_l1" ? t("answer_form_translation") : "");
    const alt = forms.alt ? `${t("answer_form_alt")} ${forms.alt}` : "";
    const meta = question.translation || main;
    return `
      <div class="qmain">
        <div class="qword">${promptHtml(question, hint)}</div>
        ${questionFormsHtml(question)}
        ${meta ? `<div class="qmeta">${escapeHtml(meta)}</div>` : ""}
        ${alt ? `<div class="qalt">${escapeHtml(alt)}</div>` : ""}
        ${question.is_retry || question.is_drill ? `<span class="qretry">↻ ${escapeHtml(t("is_retry"))}</span>` : ""}
        ${aidHtml(question)}
      </div>`;
  }

  // The article is coloured only where the server says the gender is already on
  // screen (`gender_known`); in the modes that ask for the article, colouring it
  // would hand the answer over (§8.4).
  function promptHtml(question, hint) {
    const prompt = String(question.prompt || "");
    const [head, ...rest] = prompt.split(" ");
    const gender = ARTICLE_GENDER[head.toLowerCase()];
    if (!hint.gender_known || !gender || !rest.length) return escapeHtml(prompt);
    return `<span class="art ${gender}">${escapeHtml(head)}</span> ${escapeHtml(rest.join(" "))}`;
  }

  // De→L1 shows the word's other forms with the question, the way the chat does.
  function questionFormsHtml(question) {
    const forms = question.forms;
    if (!forms) return "";
    const parts = [];
    if (forms.plural) parts.push(`pl. die ${forms.plural}`);
    if (forms.prateritum) parts.push(forms.prateritum);
    if (forms.partizip2) parts.push(forms.partizip2);
    return parts.length ? `<div class="qforms">${escapeHtml(parts.join(" · "))}</div>` : "";
  }

  // The learner's own image and example, shown with a De→L1 question as a
  // memory aid (user_guide §3). The server sends them only where they cannot
  // give the answer away.
  function aidHtml(question) {
    const media = question.media;
    if (!media) return "";
    const lines = [media.image, media.example].filter(Boolean);
    return lines.length
      ? `<div class="qaid">${lines.map((line) => `<div>${escapeHtml(line)}</div>`).join("")}</div>`
      : "";
  }

  function previewHtml(question) {
    const card = question.card || {};
    return `<div class="scroll">${App.renderCardHead(card)}${App.renderCardBody(card)}</div>`;
  }

  // ---------------------------------------------------------------------
  // The bottom bar, per answer kind
  // ---------------------------------------------------------------------

  function footHtml(kind) {
    if (kind === KIND_ARTICLE) {
      return `
        <div class="tapnext" data-qtap hidden></div>
        <div class="artbtns">${ARTICLES.map(
          (article) => `<button class="artbtn" data-article="${article}">${article}</button>`
        ).join("")}</div>`;
    }
    if (kind === KIND_NONE) {
      return `<div class="answerbar"><div class="qacts">
        <button class="qact primary" data-act="next">${escapeHtml(t("learning_next"))}</button>
        <button class="qact" data-act="skip">${escapeHtml(t("learning_skip"))}</button>
        <button class="qact" data-act="stop">${escapeHtml(t("action_stop"))}</button>
      </div></div>`;
    }
    // The input carries every attribute in §8.5a: each one fixes a concrete way a
    // mobile keyboard would otherwise get in the way of German.
    return `
      <div class="tapnext" data-qtap hidden></div>
      <div class="answerbar">
        <form>
          <div class="inp"><input type="text" enterkeyhint="send" autocomplete="off"
                 autocorrect="off" autocapitalize="off" spellcheck="false"
                 placeholder="${escapeHtml(t("answer_placeholder"))}"></div>
        </form>
        <div class="qacts" data-qacts></div>
      </div>`;
  }

  // The three actions under a typed question. `input_err` is absent here on
  // purpose: it takes back the answer before this one, so it belongs on the
  // result screen where that answer is still on display.
  function questionActs() {
    const middle = q.mode === LEARNING ? ["skip", "learning_skip"] : ["send_learn", "action_send_learn"];
    return actsHtml([["idk", "action_idk"], middle, ["stop", "action_stop"]]);
  }

  function resultActs(correct) {
    const acts = [];
    // Only a wrong answer can be a typo, and only in a mode that is typed into.
    if (!correct && q.footKind === KIND_TEXT) acts.push(["input_err", "action_input_err"]);
    acts.push(["send_learn", "action_send_learn"], ["stop", "action_stop"]);
    return actsHtml(acts);
  }

  function actsHtml(acts) {
    return acts
      .map(([act, key]) => `<button class="qact" data-act="${act}">${escapeHtml(t(key))}</button>`)
      .join("");
  }

  function setActs(html) {
    const acts = part("qacts");
    if (!acts) return;
    acts.innerHTML = html;
    wireActs(acts);
  }

  function showTapHint() {
    const hint = part("qtap");
    if (!hint) return;
    hint.textContent = q.footKind === KIND_TEXT ? t("tap_next_enter") : t("tap_next");
    hint.hidden = false;
  }

  function wireArticleButtons() {
    q.screen.querySelectorAll("[data-article]").forEach((btn) => {
      btn.onclick = () => {
        if (q.phase !== "question") return;
        // The pick lights up immediately and keeps the accent through the result,
        // so the user's own answer stays visible where they made it (§8.4).
        btn.classList.add("pressed");
        sendAnswer(btn.dataset.article);
      };
    });
  }

  // ---------------------------------------------------------------------
  // Answering
  // ---------------------------------------------------------------------

  function onSubmit(ev) {
    ev.preventDefault();
    // Enter means two different things one after the other: send this answer,
    // then move on from its verdict (§8.5a).
    if (q.phase === "result") {
      advance();
      return;
    }
    const text = q.input ? q.input.value.trim() : "";
    if (!text) return; // an empty answer goes through "I don't know", never by Enter
    sendAnswer(text);
  }

  async function sendAnswer(text, dontKnow) {
    if (q.busy || !q.question) return;
    q.busy = true;
    const path = dontKnow ? "session/idk" : "session/answer";
    const body = { question_token: q.question.token };
    if (!dontKnow) body.text = text;
    let data;
    try {
      data = await api(path, { method: "POST", body });
    } catch (err) {
      await recover(err);
      return;
    } finally {
      q.busy = false;
    }
    if (q.input) q.input.value = "";
    showResult(data);
  }

  function showResult(data) {
    q.phase = "result";
    q.pending = data.next || null;
    const feedback = data.feedback || {};
    if (tg && tg.HapticFeedback) {
      tg.HapticFeedback.notificationOccurred(data.correct ? "success" : "error");
    }
    headerAfterAnswer();
    part("qbody").innerHTML = resultHtml(data.correct, feedback);
    if (q.footKind === KIND_ARTICLE) markArticleResult(feedback.expected);
    else setActs(resultActs(data.correct));
    showTapHint();
  }

  // The authoritative count after this answer is the one that came with the next
  // step; a drained queue reports it in the summary instead.
  function headerAfterAnswer() {
    const next = q.pending || {};
    const progress = (next.question && next.question.progress) || {};
    if (progress.total_due) {
      setHeader(Number(progress.answered || 0), Number(progress.total_due));
    } else if (next.summary) {
      setHeader(Number(next.summary.answered || 0), Number(next.summary.answered || 0));
    }
  }

  function resultHtml(correct, feedback) {
    const card = feedback.card;
    const streak = Number(feedback.streak || 0);
    const aside = correct && streak > 1 ? `🔥 ${streak} ${t("verdict_streak")}` : "";
    return `
      <div class="scroll">
        <div class="verdict ${correct ? "ok" : "err"}">
          <span class="ic">${correct ? "✓" : "✕"}</span>
          <span class="tt">${escapeHtml(t(correct ? "verdict_correct" : "verdict_wrong"))}</span>
          ${aside ? `<span class="x">${escapeHtml(aside)}</span>` : ""}
        </div>
        ${feedback.expected
          ? `<div class="expected"><div class="lbl">${escapeHtml(t("expected_answer"))}</div>
               <div class="v">${escapeHtml(feedback.expected)}</div></div>`
          : ""}
        ${feedback.completed ? `<div class="donenote">${escapeHtml(t("learning_completed"))}</div>` : ""}
        ${card ? App.renderCardHead(card) + App.renderCardBody(card) : ""}
      </div>`;
  }

  // No red on the buttons: the pressed one keeps the accent it lit up with, the
  // right one turns green, and a wrong pick reads as wrong because green is
  // somewhere else (§8.4). Both roles land on one button when the answer is right.
  function markArticleResult(expected) {
    const right = String(expected || "").split(" ")[0].toLowerCase();
    q.screen.querySelectorAll("[data-article]").forEach((btn) => {
      const article = btn.dataset.article;
      const picked = btn.classList.contains("pressed");
      btn.classList.toggle("right", article === right);
      btn.classList.toggle("dim", !picked && article !== right);
      const label = article === right ? t("article_correct") : picked ? t("article_your_pick") : "";
      btn.innerHTML = `${article}${label ? `<span class="sub">${escapeHtml(label)}</span>` : ""}`;
    });
  }

  // ---------------------------------------------------------------------
  // Moving on
  // ---------------------------------------------------------------------

  function advance() {
    const next = q.pending;
    q.pending = null;
    if (!next) return;
    if (next.question) showQuestion(next.question);
    else if (next.summary) showSummary(next.summary);
    else showEmptyAfterSession(next);
  }

  function showEmptyAfterSession(info) {
    // The queue ran dry but the session is still open so the user could read the
    // last verdict; nothing is left to ask, so it closes now.
    stopQuietly();
    setLeaveHook(null);
    showEmpty(info);
  }

  function showEmpty(info) {
    if (q.mode === LEARNING) {
      goTo(() => renderLearningEmpty(info), { root: true });
      return;
    }
    goTo(App.renderNoDue, { root: true });
  }

  // ---------------------------------------------------------------------
  // Actions
  // ---------------------------------------------------------------------

  const ACTIONS = {
    idk: () => sendAnswer("", true),
    stop: stopSession,
    next: () => advanceLearning("session/next"),
    skip: () => advanceLearning("session/skip"),
    send_learn: sendLearn,
    input_err: applyInputErr,
  };

  // "Next" and "skip" spend the question token exactly as an answer does (§4.4):
  // both write, and a double tap would otherwise mark two words reviewed while
  // the user saw one card.
  async function advanceLearning(path) {
    if (q.busy || !q.question) return;
    q.busy = true;
    let data;
    try {
      data = await api(path, { method: "POST", body: { question_token: q.question.token } });
    } catch (err) {
      await recover(err);
      return;
    } finally {
      q.busy = false;
    }
    q.pending = data.next || null;
    advance();
  }

  async function sendLearn() {
    const wordId = q.question && q.question.word_id;
    if (!wordId) return;
    try {
      await api("session/send_learn", { method: "POST", body: { word_id: wordId } });
    } catch (err) {
      handleApiError(err);
      return;
    }
    toast(t("send_learn_done"));
  }

  async function applyInputErr() {
    let data;
    try {
      data = await api("session/input_err", { method: "POST", body: {} });
    } catch (err) {
      handleApiError(err);
      return;
    }
    if (!data.applied) {
      toast(t("input_err_none"));
      return;
    }
    // The answer now counts as correct, so the verdict on screen has to say so.
    const verdict = q.screen.querySelector(".verdict");
    if (verdict) {
      verdict.classList.replace("err", "ok");
      verdict.querySelector(".ic").textContent = "✓";
      verdict.querySelector(".tt").textContent = t("verdict_correct");
    }
    setActs(resultActs(true));
    toast(t("input_err_done"));
  }

  // A spent or stale token is not an error the user should read about (§7.7):
  // the question on screen is simply out of date, so it is fetched again.
  async function recover(err) {
    if (!(err instanceof ApiError) || err.status !== 409 || err.code !== "stale_question") {
      handleApiError(err);
      return;
    }
    let data;
    try {
      data = await api("session/question");
    } catch (again) {
      handleApiError(again);
      return;
    }
    if (data.question) showQuestion(data.question);
    else showEmptyAfterSession({});
  }

  // ---------------------------------------------------------------------
  // Summary (§7.4) - FSRS modes only; weak and learning have none
  // ---------------------------------------------------------------------

  function showSummary(summary) {
    setLeaveHook(null);
    goTo(() => renderSummary(summary), { root: true });
  }

  function renderSummary(summary) {
    const answered = Number(summary.answered || 0);
    const correct = Number(summary.correct || 0);
    const share = answered ? Math.round((correct / answered) * 100) : 0;
    const cells = [
      [answered, "summary_answered"],
      [correct, "summary_correct"],
      [Number(summary.wrong || 0), "summary_wrong"],
      [Number(summary.retries || 0), "summary_retries"],
    ];
    const screen = el(`
      <div class="screen">
        <div class="sum">
          ${summary.effect === "fire" ? '<div class="fire">🔥</div>' : ""}
          <div class="big">${share}<small>%</small></div>
          <div class="lbl">${escapeHtml(t("summary_correct_share"))} · ${escapeHtml(
            t(App.MODE_LABEL[summary.mode] || summary.mode)
          )}</div>
          <div class="grid">
            ${cells
              .map(([value, key]) => `<div><b>${value}</b><span>${escapeHtml(t(key))}</span></div>`)
              .join("")}
          </div>
        </div>
        <div class="stack">
          <button class="bigbtn" data-again>${escapeHtml(t("summary_again"))}</button>
          <button class="bigbtn ghost" data-menu>${escapeHtml(t("back_to_menu"))}</button>
        </div>
      </div>`);
    mount(screen);
    screen.querySelector("[data-again]").addEventListener("click", () => start(summary.mode || q.mode));
    screen.querySelector("[data-menu]").addEventListener("click", toMenu);
  }

  // ---------------------------------------------------------------------
  // Learning: the empty batch and its top-up (§7.3)
  // ---------------------------------------------------------------------

  function clearCountdown() {
    clearInterval(q.countdown);
    q.countdown = null;
  }

  function renderLearningEmpty(info) {
    const noWords = info.empty === EMPTY_NO_WORDS;
    const screen = App.stateScreen({
      icon: noWords ? "📭" : "⏳",
      title: t(noWords ? "state_no_words" : "state_no_due"),
      hint: noWords ? t("state_no_words_hint") : t("state_no_due_hint"),
      // Topping the batch up is the answer to an empty batch; it is not the answer
      // to a topic filter that holds no words at all.
      ctaLabel: noWords ? t("back_to_menu") : t("learning_add_batch"),
      onCta: noWords ? toMenu : fillBatch,
      cta2Label: noWords ? "" : t("back_to_menu"),
      onCta2: toMenu,
    });
    mount(screen);
    if (!noWords && info.next_due) startCountdown(screen, info.next_due);
  }

  // The server sends the due time, not a countdown: the app has a client that can
  // keep the number ticking, which a chat message cannot (learning_flow.py).
  function startCountdown(screen, nextDue) {
    const line = screen.querySelector("p");
    if (!line) return;
    const due = Date.parse(nextDue);
    if (Number.isNaN(due)) return;
    const tick = () => {
      const left = Math.round((due - Date.now()) / 1000);
      if (left <= 0) {
        clearCountdown();
        line.textContent = t("state_no_due_hint");
        return;
      }
      const minutes = Math.floor(left / 60);
      line.textContent = `${t("state_next_due")} ${minutes ? `${minutes}m ` : ""}${left % 60}s`;
    };
    tick();
    clearCountdown();
    q.countdown = setInterval(tick, 1000);
    setLeaveHook(clearCountdown);
  }

  async function fillBatch() {
    let data;
    try {
      data = await api("learning/fill", { method: "POST", body: {} });
    } catch (err) {
      handleApiError(err);
      return;
    }
    if (!data.added) {
      toast(t(FILL_REASON_KEY[data.reason] || "state_no_words"));
      return;
    }
    toast(`${t("learning_filled")}: ${data.added}`);
    clearCountdown();
    start(LEARNING);
  }

  App.quiz = { start, renderLearningEmpty };
})();
