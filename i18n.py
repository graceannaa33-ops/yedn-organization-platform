"""Minimal translation layer so Kiswahili (or another language) can be added later.

Templates call  {{ _('Dashboard') }}.  The English text is the key.
To add a language, create translations/<code>.json mapping English -> translation
and add the code to SUPPORTED_LANGUAGES. Missing keys fall back to English.
"""
import json
import os

from flask import session

SUPPORTED_LANGUAGES = {"en": "English", "sw": "Kiswahili"}
_catalogs = {}
_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "translations")


def _load(code):
    if code not in _catalogs:
        path = os.path.join(_DIR, f"{code}.json")
        try:
            with open(path, encoding="utf-8") as fh:
                _catalogs[code] = json.load(fh)
        except (OSError, ValueError):
            _catalogs[code] = {}
    return _catalogs[code]


def current_language():
    code = session.get("lang", "en")
    return code if code in SUPPORTED_LANGUAGES else "en"


def gettext(text):
    code = current_language()
    if code == "en":
        return text
    return _load(code).get(text, text)
