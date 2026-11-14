"""Thin LLM wrapper. If LLM_API_KEY is unset (or the call/parse fails) we return the deterministic fallback."""
import json, os

API_KEY = os.getenv("LLM_API_KEY")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
_client = None


def enabled() -> bool:
    return bool(API_KEY)


def _c():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=8.0)
    return _client


def llm_json(system: str, user: str, fallback):
    if not enabled():
        return fallback()
    try:
        r = _c().chat.completions.create(
            model=MODEL, temperature=0, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        return json.loads(r.choices[0].message.content)
    except Exception as e:                      # bad JSON, rate limit, no network...
        print(f"[llm] falling back: {e}")
        return fallback()


def llm_text(system: str, user: str, fallback):
    if not enabled():
        return fallback()
    try:
        r = _c().chat.completions.create(model=MODEL, temperature=0.2,
                                         messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        return r.choices[0].message.content.strip()
    except Exception as e:
        print(f"[llm] falling back: {e}")
        return fallback()
