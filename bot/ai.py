import os

from openai import OpenAI


def _get_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing in .env")
    return OpenAI(api_key=api_key)


def _get_model():
    return os.getenv("OPENAI_MODEL", "gpt-5-mini")


def generate_fields(prompt_text):
    client = _get_client()
    model = _get_model()

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt_text}],
    )
    return response.choices[0].message.content
