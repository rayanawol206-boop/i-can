"""Language order and the local phrase boards.

English letters, suggestions, and quick phrases stay in ``keyboard_ui``.
Amharic, Afaan Oromo, and Tigrinya come from ``local_phrases.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CATALOG_PATH = APP_DIR / "local_phrases.json"

# The switch cycles in this order and then returns to English.
ORDER = ("en", "am", "om", "ti")
FONT_CANDIDATES = ("Ebrima", "Nyala", "Noto Sans Ethiopic")


def load_catalog(path: Path | None = None) -> dict:
    source = path or CATALOG_PATH
    return json.loads(source.read_text(encoding="utf-8"))


def next_language(code: str) -> str:
    try:
        index = ORDER.index(code)
    except ValueError:
        index = 0
    return ORDER[(index + 1) % len(ORDER)]


def language_name(code: str, catalog: dict) -> str:
    if code == "en":
        return "English"
    return str(catalog[code]["name"])


def language_caption(code: str, catalog: dict) -> str:
    return f"LANGUAGE · {language_name(code, catalog)}"


def pick_script_font(families: set[str] | list[str]) -> str:
    available = set(families)
    for name in FONT_CANDIDATES:
        if name in available:
            return name
    return "Segoe UI"


def category_board(code: str, catalog: dict, caption: str) -> tuple[list, list]:
    items = [("category", cat["id"], cat["label"]) for cat in catalog[code]["categories"]]
    rows = _pairs(items)
    rows.append([("language", "", caption)])
    names = [" · ".join(label for _kind, _value, label in row) for row in rows[:-1]]
    names.append("language")
    return rows, names


def phrase_board(code: str, catalog: dict, category_id: str, caption: str) -> tuple[list, list]:
    language = catalog[code]
    category = next(cat for cat in language["categories"] if cat["id"] == category_id)
    items = [("phrase", text, text) for text in category["phrases"]]
    rows = _pairs(items)
    rows.append([("back", "", language["back"]), ("language", "", caption)])
    names = [" · ".join(label for _kind, _value, label in row) for row in rows[:-1]]
    names.append(language["back"])
    return rows, names


def cache_lines(catalog: dict) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    for code, language in catalog.items():
        for category in language["categories"]:
            for phrase in category["phrases"]:
                lines.append((code, phrase))
    return lines


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
