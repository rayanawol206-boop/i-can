"""Health and comfort answers.

A configured API key and a network connection produce a short live answer
in the active language. Otherwise the built-in guide is used for the
ready-made questions, and a plain message is shown for anything else.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from pathlib import Path


log = logging.getLogger("ican.assistant")

APP_DIR = Path(__file__).resolve().parent
GUIDE_PATH = APP_DIR / "comfort_guides.json"
ENV_PATH = APP_DIR / ".env"

LANGUAGE_NAMES = {
    "en": "English",
    "am": "Amharic",
    "om": "Afaan Oromo",
    "ti": "Tigrinya",
}

ASK_TYPED = {
    "en": "Ask My Typed Sentence",
    "am": "የጻፍኩትን ጠይቅ",
    "om": "Waan barreesse gaafadhu",
    "ti": "ዝጸሓፍኩ ሕተት",
}
NEED_KEY = {
    "en": "The live assistant needs an internet connection and an API key in the .env file. You can still choose a ready-made question for a built-in guide.",
    "am": "የቀጥታ ረዳቱ ኢንተርኔት እና በ .env ውስጥ የኤፒአይ ቁልፍ ይፈልጋል። ዝግጁ የሆኑ ጥያቄዎች ግን አሁንም መመሪያ አላቸው።",
    "om": "Gargaaraan kallattiin interneetii fi furtuu API .env keessaa barbaada. Gaaffileen qophaa'an garuu qajeelfama qabu.",
    "ti": "እቲ ቀጥታዊ ሓጋዚ ኢንተርነትን ኣብ .env መፍትሕ APIን የድሊ። ዝተዳለዉ ሕቶታት ግን መምርሒ ኣለዎም።",
}
EMPTY_SENTENCE = {
    "en": "The sentence box is empty. Type with the keyboard, or choose a ready-made question.",
    "am": "የአረፍተ ነገሩ ሳጥን ባዶ ነው። በቁልፍ ሰሌዳው ይጻፉ ወይም ዝግጁ ጥያቄ ይምረጡ።",
    "om": "Sanduuqin himaa duwwaadha. Kiboodii fayyadami ykn gaaffii qophaa'e filadhu.",
    "ti": "ሳጹን ሓሳብ ባዶ እዩ። ብመፍትሕ ጽሓፍ ወይ ዝተዳለወ ሕቶ ምረጽ።",
}
FAILED = {
    "en": "The assistant could not answer just now. Please try again in a moment.",
    "am": "ረዳቱ አሁን መልስ መስጠት አልቻለም። እባክዎ ትንሽ ቆይተው ደግመው ይሞክሩ።",
    "om": "Gargaaraan amma deebii kennuu hin dandeenye. Maaloo xiqqo booda irra deebi'i.",
    "ti": "እቲ ሓጋዚ ሕጂ መልሲ ክህብ ኣይከኣለን። ቁሩብ ጸኒሕካ ደጊምካ ፈትን።",
}
STATUS_LIVE = {
    "en": "Live answer",
    "am": "የቀጥታ መልስ",
    "om": "Deebii kallattii",
    "ti": "ቀጥታዊ መልሲ",
}
STATUS_GUIDE = {
    "en": "Built-in guide. Add API_KEY to .env for a live answer.",
    "am": "የተዘጋጀ መመሪያ። ለቀጥታ መልስ API_KEYን በ .env ይጨምሩ።",
    "om": "Qajeelfama keessaa. Deebii kallattiif API_KEY .env keessatti dabali.",
    "ti": "ዝተዳለወ መምርሒ። ንቀጥታዊ መልሲ API_KEY ኣብ .env ወስኽ።",
}
STATUS_ASKING = {
    "en": "Asking the assistant…",
    "am": "ረዳቱን እየጠየቅን ነው…",
    "om": "Gargaaraa gaafachaa jirra…",
    "ti": "ነቲ ሓጋዚ ንሕተት ኣለና…",
}


@dataclass
class AssistantReply:
    text: str
    speak: str
    status: str


def load_guides(path: Path | None = None) -> dict:
    source = path or GUIDE_PATH
    return json.loads(source.read_text(encoding="utf-8"))


def load_settings(path: Path | None = None) -> dict:
    settings = {
        "API_KEY": "",
        "API_BASE": "https://api.openai.com/v1",
        "API_MODEL": "gpt-4o-mini",
        "API_PROVIDER": "",
    }
    source = path or ENV_PATH
    if not source.exists():
        return settings
    for raw in source.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        settings[key.strip()] = value.strip().strip('"').strip("'")
    return settings


def question_board(language: str, guides: dict, back: str, caption: str) -> tuple[list, list]:
    items = [("ai", preset["id"], _text(preset["label"], language)) for preset in guides["presets"]]
    items.append(("ai", "typed", ASK_TYPED.get(language, ASK_TYPED["en"])))
    rows = _pairs(items)
    rows.append([("back", "", back), ("language", "", caption)])
    return rows, [" · ".join(label for _kind, _value, label in row) for row in rows]


def asking_status(language: str) -> str:
    return STATUS_ASKING.get(language, STATUS_ASKING["en"])


STORY_ASK = "Tell a gentle, hopeful story of about 120 words."
STORY = {
    "en": "Once there was a small lamp in a quiet room. The night felt long, but the lamp stayed lit. A friend sat beside it and said, you do not have to shine for the whole world. Shine for this moment. The room grew warmer. Morning came, and the lamp was still there, and so was the friend.",
    "am": "በጸጥታ ክፍል ውስጥ ትንሽ መብራት ነበር። ሌሊቱ ረጅም ቢሆንም መብራቱ አልጠፋም። ጓደኛው አጠገቡ ተቀምጦ አለ፣ ለመላው ዓለም ማብራት አይጠበቅብህም። ለዚህ ቅጽበት ብቻ ብርሃን ሁን። ክፍሉ ሞቀ። ጠዋት መጣ፣ መብራቱም ጓደኛውም እዚያው ነበሩ።",
    "om": "Kutaa callisaa keessatti ifa xixiqqoon ture. Halkan dheeraa ta'us ifni hin dhaabbanne. Hiriyyaan bira taa'ee, addunyaa hundaaf ifuu hin qabdu jedhe. Yeroo kana qofa ifi. Kutaan ho'e. Ganamaan dhufe, ifni fi hiriyyaanis achuma turan.",
    "ti": "ኣብ ጸጥ ዝበለ ክፍሊ ንእሽቶ መብራህቲ ነበረ። ለይቲ ነዊሕ እንተነበረ መብራህቲ ኣይጠፍአን። ዓርኪ ኣብ ጥቓኡ ተቐሚጡ፣ ንዓለም ኩላ ክትበርህ ኣይኮነን። ነዚ ሕጂ እዚ በሪህ። ክፍሊ መኺኡ። ንጋሆ መጺኡ፣ መብራህትን ዓርክን ኣብኡ ነበሩ።",
}
READY = {
    "en": "Ready. Press Hear when you want this read aloud.",
    "am": "ተዘጋጅቷል። እንዲነበብልዎ ሲፈልጉ Hear ይጫኑ።",
    "om": "Qophiidha. Yoo dhageeffachuu barbaadde Hear cuqaasi.",
    "ti": "ድሉው እዩ። ክንበብ እንተደሊኻ Hear ጠውቕ።",
}


def answer(language: str, preset_id: str, typed: str, app_dir: Path | None = None) -> AssistantReply:
    """Answer one scanned question. Never raises for a missing key or a dead network."""
    root = app_dir or APP_DIR
    guides = load_guides(root / "comfort_guides.json")
    code = language if language in LANGUAGE_NAMES else "en"
    preset = _preset(guides, preset_id)
    question = typed.strip() if preset_id == "typed" else _text(preset["ask"], code) if preset else ""
    if preset_id == "typed" and not question:
        text = EMPTY_SENTENCE.get(code, EMPTY_SENTENCE["en"])
        return AssistantReply(text, text, text)

    settings = load_settings(root / ".env")
    profile = _profile(root / "profile.json")
    if preset_id == "story":
        live = _try_live(STORY_ASK, code, settings, profile, story=True)
        text = live or STORY.get(code, STORY["en"])
        return AssistantReply(text, text, READY.get(code, READY["en"]))
    key = settings.get("API_KEY", "").strip()
    live = _try_live(question, code, settings, profile, story=False) if key else ""
    if live:
        return AssistantReply(live, live, READY.get(code, READY["en"]))

    guide = _guide_for(guides, code, preset, question)
    if guide:
        return AssistantReply(guide, guide, STATUS_GUIDE.get(code, STATUS_GUIDE["en"]))
    if key:
        text = FAILED.get(code, FAILED["en"])
        return AssistantReply(text, text, text)
    text = NEED_KEY.get(code, NEED_KEY["en"])
    return AssistantReply(text, text, text)


def _preset(guides: dict, preset_id: str) -> dict | None:
    for preset in guides.get("presets", []):
        if preset.get("id") == preset_id:
            return preset
    return None


def _guide_for(guides: dict, language: str, preset: dict | None, question: str) -> str:
    if preset:
        return _text(preset["guide"], language)
    folded = question.casefold()
    for item in guides.get("presets", []):
        for word in item.get("keywords", []):
            if word and word.casefold() in folded:
                return _text(item["guide"], language)
    return ""


def _profile(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _try_live(question: str, language: str, settings: dict, profile: dict, story: bool) -> str:
    if not settings.get("API_KEY", "").strip() or not question or not _online():
        return ""
    try:
        return _ask_model(question, language, settings, profile, story)
    except Exception:
        log.exception("assistant request failed")
        return ""


def _ask_model(question: str, language: str, settings: dict, profile: dict | None = None, story: bool = False) -> str:
    language_name = LANGUAGE_NAMES.get(language, "English")
    if story:
        instruction = (
            f"Tell one gentle, hopeful story in {language_name}. About 120 words. "
            f"Simple words. No diagnosis and no medicine."
        )
    else:
        instruction = (
            f"You are a gentle companion for a person who may be ill and speaks with a switch. "
            f"Answer in {language_name}. Use 3 to 5 short, warm sentences and simple words. "
            f"Do not diagnose, name a dose, or change a treatment. "
            f"If it could be an emergency, say to call a caregiver or nurse now. "
            f"You are not a doctor."
        )
    remembered = _memory(profile or {})
    if remembered:
        instruction = f"{instruction} {remembered}"
    provider = settings.get("API_PROVIDER", "").strip().lower()
    base = settings.get("API_BASE", "https://api.openai.com/v1").rstrip("/")
    model = settings.get("API_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
    if provider == "gemini" or "generativelanguage.googleapis.com" in base or model.startswith("gemini"):
        text = _gemini(instruction, question, settings.get("API_KEY", ""), model, 500 if story else 280)
    else:
        text = _openai(instruction, question, settings.get("API_KEY", ""), base, model)
    return _shorten(text, 900 if story else 520)


def _memory(profile: dict) -> str:
    name = str(profile.get("name") or "").strip()
    origin = str(profile.get("origin") or "").strip()
    condition = str(profile.get("condition") or "").strip()
    parts = []
    if name:
        parts.append(f"Their name is {name}.")
    if origin:
        parts.append(f"They are from {origin}.")
    if condition:
        parts.append(f"They asked you to remember they are living with {condition}. Do not invent extra symptoms.")
    return " ".join(parts)


def _openai(instruction: str, question: str, key: str, base: str, model: str) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": instruction},
            {"role": "user", "content": question},
        ],
        "max_tokens": 280,
        "temperature": 0.4,
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        data = json.loads(response.read().decode("utf-8"))
    return str(data["choices"][0]["message"]["content"])


def _gemini(instruction: str, question: str, key: str, model: str, tokens: int = 280) -> str:
    import time
    import urllib.error

    names = []
    for name in (model, "gemini-3.8-flash", "gemini-flash-latest"):
        if name and name not in names:
            names.append(name if str(name).startswith("gemini") else "gemini-3.8-flash")
    payload = json.dumps(
        {
            "contents": [{"role": "user", "parts": [{"text": f"{instruction}\n\n{question}"}]}],
            "generationConfig": {"maxOutputTokens": tokens, "temperature": 0.4},
        }
    ).encode("utf-8")
    last = None
    for chosen in names:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{chosen}:generateContent"
        for attempt in (1, 2):
            request = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json", "X-goog-api-key": key},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=25) as response:
                    data = json.loads(response.read().decode("utf-8"))
                parts = data["candidates"][0]["content"]["parts"]
                return " ".join(str(part.get("text", "")) for part in parts)
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in {429, 503} and attempt == 1:
                    time.sleep(0.8)
                    continue
                if exc.code in {404, 429, 503}:
                    break
                raise
    if last is not None:
        raise last
    raise RuntimeError("no gemini model answered")


def _online() -> bool:
    from youtube_player import internet_ok

    return internet_ok(3.0)


def _shorten(text: str, limit: int = 520) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    for mark in (". ", "። ", "! ", "? "):
        pos = cut.rfind(mark)
        if pos > 80:
            return cut[: pos + 1].strip()
    return cut.strip()


def _text(table: dict, language: str) -> str:
    return str(table.get(language) or table.get("en") or "")


def _pairs(items: list) -> list:
    rows = []
    chunk: list = []
    for item in items:
        chunk.append(item)
        if len(chunk) == 2:
            rows.append(chunk)
            chunk = []
    if chunk:
        rows.append(chunk)
    return rows
