import os


def _get_client():
    # Import openai lazily: importing this module (and its importers, e.g.
    # add_words) must not require the openai package — only actually generating
    # fields does. Keeps non-AI code paths (and their tests) dependency-free.
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing in .env")
    return OpenAI(api_key=api_key)


def _get_model():
    return os.getenv("OPENAI_MODEL", "gpt-5-mini")


def _supports_temperature(model):
    """Reasoning models (o-series, gpt-5*) reject temperature != 1; others accept it."""
    return not (model or "").lower().startswith(("o1", "o3", "o4", "gpt-5"))


def generate_fields(prompt_text):
    client = _get_client()
    model = _get_model()

    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": prompt_text}],
        # Prompts always ask for "a JSON object" — force valid JSON, no stray prose.
        "response_format": {"type": "json_object"},
    }
    if _supports_temperature(model):
        # Deterministic decoding for factual extraction (articles/plurals/verb forms):
        # kills variance-induced wrong guesses and makes results reproducible.
        kwargs["temperature"] = 0

    response = client.chat.completions.create(**kwargs)
    return response.choices[0].message.content
