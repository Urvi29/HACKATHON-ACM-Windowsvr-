"""Cloud LLM client. Receives ONLY the payload built by privacy.prepare_payload (redacted metadata).
Without ANTHROPIC_API_KEY the app stays fully functional using on-device rule-based suggestions."""
import json
import os
import re

from . import db


def _load_env():
    try:
        from dotenv import load_dotenv

        load_dotenv(db.ROOT / ".env")
    except Exception:
        pass


_load_env()

GEMINI_MODEL = os.environ.get("ME3_GEMINI_MODEL", "gemini-3.5-flash-lite")
# tried in order when the chosen model is overloaded (503/429/timeout) or retired (404)
GEMINI_FALLBACKS = ["gemini-3.1-flash-lite", "gemini-3.8-flash", "gemini-flash-latest"]
CLAUDE_MODEL = os.environ.get("ME3_LLM_MODEL", "claude-haiku-5-5")

SYSTEM = (
    "You are a file-organisation assistant. You receive a JSON list of files with redacted paths and metadata "
    "(placeholders like <EMAIL_1> hide personal data; never try to infer them). Propose a concise, practical folder "
    "structure and cleanup actions for the user's OWN content: documents, downloads, screenshots, photos, notes. "
    "Rules: never suggest moving, renaming or deleting program, game or system files (DLL/EXE/libraries, Wine prefixes, "
    "game installs, app data); never split code projects or machine-learning datasets (images and their label files must "
    "stay together); prefer moving whole folders over individual files; use ~ paths. "
    "Keep titles under 8 words and details under 35 words. Reply with ONLY JSON: "
    '{"summary": str, "suggestions": [{"title": str, "detail": str, "folder": str, "example_files": [str]}]} '
    "with at most 6 suggestions and at most 3 example_files each."
)
SCHEMA = {  # Ollama/Gemini structured output: the reply is forced into this shape
    "type": "object", "required": ["summary", "suggestions"],
    "properties": {
        "summary": {"type": "string"},
        "suggestions": {"type": "array", "maxItems": 6, "items": {
            "type": "object", "required": ["title", "detail", "folder"],
            "properties": {"title": {"type": "string"}, "detail": {"type": "string"}, "folder": {"type": "string"},
                           "example_files": {"type": "array", "maxItems": 3, "items": {"type": "string"}}}}},
    },
}


def parse_reply(text: str) -> dict:
    """Model output -> {summary, suggestions}. Tolerates code fences, chatter and a reply cut off mid-list
    (keeps every complete suggestion). Raises ValueError when nothing usable is in it."""
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    start = t.find("{")
    if start < 0:
        raise ValueError("no JSON object in the reply")
    t = t[start:]
    try:
        data, _ = json.JSONDecoder().raw_decode(t)
    except ValueError:
        data = None
        dec = json.JSONDecoder()
        summ = re.search(r'"summary"\s*:\s*("(?:[^"\\]|\\.)*")', t)
        sugg, i = [], t.find("[", t.find('"suggestions"')) + 1
        while 0 < i < len(t):  # salvage each complete suggestion object
            j = t.find("{", i)
            if j < 0:
                break
            try:
                obj, end = dec.raw_decode(t, j)
            except ValueError:
                break
            sugg.append(obj)
            i = end
        if sugg or summ:
            data = {"summary": json.loads(summ.group(1)) if summ else "", "suggestions": sugg, "partial": True}
        if data is None:
            raise
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    out = [{"title": str(x.get("title", "")).strip(), "detail": str(x.get("detail", "")).strip(), "folder": str(x.get("folder", "")).strip(),
            "example_files": [str(f) for f in (x.get("example_files") or [])][:3]}
           for x in (data.get("suggestions") or []) if isinstance(x, dict) and x.get("title")]
    if data.get("partial") and not out:
        raise ValueError("the reply broke off before any complete suggestion")
    return {"summary": str(data.get("summary") or "").strip(), "suggestions": out[:6], **({"partial": True} if data.get("partial") else {})}


OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if not OLLAMA_URL.startswith("http"):
    OLLAMA_URL = "http://" + OLLAMA_URL
OLLAMA_MODEL = os.environ.get("ME3_OLLAMA_MODEL", "gemma4:e4b")  # text + vision, fits an 8 GB GPU


def _ollama_ready() -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=2) as r:
            names = {m["name"] for m in json.load(r).get("models", [])}
        return OLLAMA_MODEL in names or f"{OLLAMA_MODEL}:latest" in names
    except Exception:  # noqa: BLE001
        return False


def provider() -> str | None:
    """Privacy first: the on-device Ollama model is the default. Cloud providers are used ONLY when
    ME3_LLM_PROVIDER=gemini|anthropic is set explicitly (and their key exists); there is no silent cloud fallback."""
    _load_env()
    want = os.environ.get("ME3_LLM_PROVIDER", "ollama").lower()
    if want == "gemini" and os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if want == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "ollama" if _ollama_ready() else None


def is_local() -> bool:
    return provider() == "ollama"


def available() -> bool:
    return provider() is not None


def model_name() -> str:
    return {"ollama": OLLAMA_MODEL, "gemini": GEMINI_MODEL}.get(provider() or "", CLAUDE_MODEL)


MODEL = CLAUDE_MODEL  # kept for backward compatibility


class LLMError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _gemini_call(model: str, payload: dict, body_override: dict | None = None) -> str:
    import urllib.error
    import urllib.request

    body = json.dumps(body_override or {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps({"files": payload["items"]})}]}],
        "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 4000},
    }).encode()
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=body, method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]},
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise LLMError(e.code, e.read().decode(errors="replace")[:300]) from e
    except (TimeoutError, urllib.error.URLError) as e:
        raise LLMError(504, f"{model} timed out") from e
    return "".join(p.get("text", "") for c in data.get("candidates", [])[:1]
                   for p in c.get("content", {}).get("parts", []))


def _gemini(payload: dict, body_override: dict | None = None) -> str:
    last: LLMError | None = None
    for model in [GEMINI_MODEL, *[m for m in GEMINI_FALLBACKS if m != GEMINI_MODEL]]:
        try:
            return _gemini_call(model, payload, body_override)
        except LLMError as e:
            if e.status in (401, 403):
                raise  # bad key: another model will not help
            last = e
    raise last  # type: ignore[misc]


def organise(payload: dict) -> dict:
    """Send the (already redacted) payload to the configured provider. Caller must have shown it to the user."""
    if provider() == "ollama":
        msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps({"files": payload["items"]})}]
        last = None
        for attempt in range(2):  # small local models occasionally emit broken JSON: one retry, then a clear error
            text = _ollama_chat(msgs, num_predict=2500, fmt=SCHEMA, temperature=0.1 if attempt == 0 else 0.0)
            try:
                return parse_reply(text)
            except ValueError as e:
                last = e
        raise LLMError(502, f"its answer was not valid JSON twice in a row ({last}); press the button again")
    elif provider() == "gemini":
        text = _gemini(payload)
    else:
        import anthropic

        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=1500, system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps({"files": payload["items"]})}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    try:
        return parse_reply(text)
    except ValueError as e:
        raise LLMError(502, f"the AI's answer was not valid JSON ({e}); press the button again") from e


def _ollama_chat(messages: list[dict], timeout: int = 180, num_predict: int = 1500, fmt=None, temperature: float = 0.1) -> str:
    import urllib.error
    import urllib.request

    body = json.dumps({"model": OLLAMA_MODEL, "messages": messages, "stream": False, "think": False, "format": fmt or "json",
                       "keep_alive": "2m",
                       "options": {"temperature": temperature, "num_predict": num_predict, "num_ctx": 8192}}).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)["message"]["content"]
    except urllib.error.HTTPError as e:
        raise LLMError(e.code, e.read().decode(errors="replace")[:300]) from e
    except (TimeoutError, urllib.error.URLError) as e:
        raise LLMError(504, f"Ollama did not answer: {e}") from e
