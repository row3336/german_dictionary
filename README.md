# German Dictionary Bot

## Overview
Owner-only Telegram bot for building a German dictionary and running quizzes. It supports:
- Manual and AI-assisted word import
- SM2-based long-term scheduling
- Session-retry for short-term reinforcement
- Weak-mode remediation and review scheduling for forms
- Synonyms management
- Stats and dumps

## Setup
1) Create a bot in @BotFather and get `BOT_TOKEN`.
2) Copy `.env.example` to `.env` and fill values.
3) Optional: add `OPENAI_API_KEY` and `OPENAI_MODEL` to enable AI helpers.
4) Install deps:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run
```powershell
python -m bot.main
```

## Access Control
The bot is owner-only. All actions require `OWNER_ID` to be set and match the user.

## Main Menus
**Main menu**
- Add words
- Quiz
- Settings
- Statistics
- Info

**Quiz menu**
- L1 > De
- De > L1

**L1 > De submenu**
- Standart mode SM2
- Article Weak Mode
- Plural Weak Mode
- Verb Forms Weak Mode
  - Prateritum Weak Mode
  - Partizip2 Weak Mode

## Add Words Flow
### 1) Precheck (AI correction + type detection)
When adding a word list or a .txt file:
- AI precheck runs first and returns a corrected base form + type.
- Bot shows list `word [type]` for approval.
- Buttons: `Approve`, `Edit list`, `Reject list`.

**Edit list**
- You send a corrected list (one per line).
- This list bypasses precheck and goes directly to recognition.

### 2) Recognition (AI field filling)
After approval:
- Each word is processed by AI with a strict JSON prompt.
- Preview format:
  - `Nomen: der Großvater (Großväter) - дедушка`
  - `Verb: bleiben (blieb / geblieben) - оставаться`

### 3) Confirm / Clarify / Reject / Ban
- Confirm saves the word.
- Clarify AI / Clarify field allow edits.
- Reject or Ban drops it.

## Quiz Modes
### L1 > De (SM2)
- For nouns: expect `article + german + plural` (strict).
- For verbs: expected answer matches `infinitiv + partizip2`.
  - `prateritum` does not affect correctness.
  - Allowed formats: `infinitiv partizip2` or `infinitiv prateritum partizip2`.

### De > L1 (SM2)
- Expect translation in L1 (with variants normalized).

### Article Quiz (SM2)
- Nouns only: check article.

## SM2 Scheduling (Long-term)
- `select_due()` returns only `due <= today`.
- Wrong answer sets `interval=1`, `due=today+1`.
- No same-day reappearance via SM2.

## Session Retry (Short-term)
Purpose: immediate reinforcement within the current session.
- Queue: `user_data["retry_queue"][mode]`.
- Wrong answer (non-retry): word id added to queue.
- After each normal question, if `>= 3` other questions passed, one retry is shown.
- Retry does **not** update SM2.
- Total repeats per word: **4** (configurable).
- Correctness during retry does not stop repeats early.
- Queue resets when leaving Quiz.

## Weak Modes (Forms)
### Active Weak
A word is active weak if:
- `{form}_errors >= 2` OR `{form}_streak_wrong >= 2`.

### Review Weak (Post-fix reinforcement)
If a word exits active weak:
- Review starts with stage 1.
- Schedule: `+1 day` → `+2 days` → `+5 days` → finish.
- Wrong in review resets to stage 1 (+1 day).

### Sources for Weak Modes
`list_weak_words()` returns:
- Active weak words
- Review weak words due today

Priority:
1) Active weak
2) Review weak

## Synonyms
- Add/Remove/Clear groups
- Auto link by translation
- Same type only

## Statistics
- Total words
- Word counts by type
- Never answered count
- Weekly added
- Streaks
- Weak counts
- Accuracy

## Storage
SQLite DB at `data/words.db` (configurable via `DB_PATH`).
Tables:
- `words`, `progress`, `quiz_events`, `form_stats`, `word_flags`, `synonym_groups`, `word_synonyms`, `users`.

## Dumps
Settings allow DB and logs dumps (manual and scheduled).
