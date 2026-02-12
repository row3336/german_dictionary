# German Dictionary Bot (MVP skeleton)

## Setup
1) Create a bot in @BotFather and get `BOT_TOKEN`.
2) Copy `.env.example` to `.env` and fill values.
3) Optional: add `OPENAI_API_KEY` and `OPENAI_MODEL` if you want AI helpers.
3) Install deps:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run
```powershell
python -m bot.main
```

## AI helper (optional)
There is a minimal helper in `bot/ai.py` that calls the OpenAI API.
You can use it to autofill fields or validate input. Example:

```python
from bot.ai import generate_fields

prompt = "Return JSON with verb fields for: machen"
text = generate_fields(prompt)
print(text)
```

## Data format
- Main storage is SQLite: `data/words.db` (configurable via `DB_PATH`).
- On startup, the bot migrates legacy JSON from `DATA_PATH` (`./data/words.json`) if the DB is empty.
- Export sends a JSON snapshot (`EXPORT_PATH`).
- Words support CEFR levels in `cefr_level` (`A1..C2`).
- Per-user enabled levels are stored separately and used as quiz filter.

## Backfill CEFR levels
To classify existing words and fill missing CEFR levels:

```powershell
python scripts/backfill_cefr.py
```

## Next steps
- Flesh out the conversation flow for quizzes.
- Add SM2 scheduling after each answer.
- Add GPT-assisted auto-fill (optional).
