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
There is a minimal helper in `bot/ai.py` that calls the OpenAI Responses API.
You can use it to autofill fields or validate input. Example:

```python
from bot.ai import generate_fields

prompt = "Return JSON with verb fields for: machen"
text = generate_fields(prompt)
print(text)
```

## Data format
`data/words.json` stores a list of entries. See examples inside the file.

## Next steps
- Flesh out the conversation flow for quizzes.
- Add SM2 scheduling after each answer.
- Add GPT-assisted auto-fill (optional).
