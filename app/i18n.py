"""Very small translation layer.

Every language is one JSON file in ``i18n/`` (e.g. ``de.json``). English is the
reference: missing keys in other languages fall back to English. To add a
language, copy ``en.json`` to ``<code>.json``, translate the values and set
``_language_name``.
"""
import json
import os
from pathlib import Path

I18N_DIR = Path(__file__).parent / "i18n"
FALLBACK = "en"

_catalogs: dict[str, dict[str, str]] = {}


def load():
    _catalogs.clear()
    for f in sorted(I18N_DIR.glob("*.json")):
        with open(f, encoding="utf-8") as fh:
            _catalogs[f.stem] = json.load(fh)


def languages() -> dict[str, str]:
    """{code: native name} of all available languages."""
    if not _catalogs:
        load()
    return {code: cat.get("_language_name", code) for code, cat in _catalogs.items()}


def default_language() -> str:
    lang = os.environ.get("DEFAULT_LANGUAGE", FALLBACK)
    return lang if lang in languages() else FALLBACK


def pick_language(user_lang: str | None, accept_language: str | None) -> str:
    langs = languages()
    if user_lang in langs:
        return user_lang
    for part in (accept_language or "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in langs:
            return code
    return default_language()


def translate(lang: str, key: str, **kwargs) -> str:
    if not _catalogs:
        load()
    text = _catalogs.get(lang, {}).get(key)
    if text is None:
        text = _catalogs.get(FALLBACK, {}).get(key, key)
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass
    return text


def translator(lang: str):
    def t(key: str, **kwargs) -> str:
        return translate(lang, key, **kwargs)

    return t
