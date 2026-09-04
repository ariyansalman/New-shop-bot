"""Translations: one JSON file per language in locales/, and a t() lookup.

Scope, deliberately: this translates the highest-traffic user-facing
surface - the main menu, the language picker and the purchase flow - not
the whole bot. The admin panel stays English (the admin already reads
English; it's their own store). Extending coverage means adding keys to
locales/en.json and swapping the matching f-strings in handlers for t()
calls; nothing about the mechanism is specific to any one language.

The strings live in locales/*.json rather than in this file because ten
languages inline would be unreadable, and because a translator can then
work on one file without touching Python. Which languages exist, and how
they are named and ordered in the picker, is utils/languages.py.

Usage: `t('main_menu.wallet_balance', lang, balance=...)`. A missing key,
a missing language, or a template whose placeholders do not match the
arguments given all fall back to English rather than raising - a broken
translation must never be what stops a customer buying something.
"""

import json
import logging
import os

from .languages import LANGUAGES

logger = logging.getLogger(__name__)

DEFAULT_LANG = 'en'
SUPPORTED_LANGS = tuple(lang.code for lang in LANGUAGES)

_LOCALES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'locales')


def _load() -> dict:
    """Read every locale file once, at import."""
    catalogue = {}
    for code in SUPPORTED_LANGS:
        path = os.path.join(_LOCALES_DIR, f'{code}.json')
        try:
            with open(path, encoding='utf-8') as handle:
                catalogue[code] = json.load(handle)
        except (OSError, ValueError):
            # A language that cannot be read simply falls back to English
            # everywhere, which is better than refusing to start.
            logger.exception("Could not load locale %s", code)
            catalogue[code] = {}
    return catalogue


_TRANSLATIONS = _load()


def t(key: str, lang: str = DEFAULT_LANG, **kwargs) -> str:
    """Look up a key in the given language, falling back to English."""
    template = _TRANSLATIONS.get(lang, {}).get(key)
    if template is None:
        template = _TRANSLATIONS.get(DEFAULT_LANG, {}).get(key)
    if template is None:
        # An unknown key is a bug, but showing the customer a raw key is a
        # worse one than showing them nothing.
        logger.warning("Missing translation key: %s", key)
        return ""

    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        # A translation whose placeholders drifted from the English one.
        # Fall back rather than raise inside a handler.
        logger.warning("Bad placeholders in %s/%s", lang, key)
        fallback = _TRANSLATIONS.get(DEFAULT_LANG, {}).get(key, "")
        try:
            return fallback.format(**kwargs)
        except (KeyError, IndexError):
            return fallback
