"""The languages the bot offers, in the order the picker shows them.

English is pinned first as the fallback everything degrades to; the rest
follow their ISO code alphabetically, which is a stable order that does not
imply a ranking of anyone's language.

Each entry carries the name written in that language rather than in
English. Someone who cannot read the current interface is exactly the
person using this screen, so "Deutsch" helps where "German" does not.
"""

from collections import namedtuple

Language = namedtuple("Language", "code flag name")

LANGUAGES = (
    Language("en",    "🇬🇧", "English"),
    Language("am",    "🇪🇹", "አማርኛ"),
    Language("ar",    "🇸🇦", "العربية"),
    Language("bn",    "🇧🇩", "বাংলা"),
    Language("fa",    "🇮🇷", "فارسی"),
    Language("hi",    "🇮🇳", "हिन्दी"),
    Language("pt-BR", "🇧🇷", "Português (Brasil)"),
    Language("ru",    "🇷🇺", "Русский"),
    Language("vi",    "🇻🇳", "Tiếng Việt"),
    Language("zh-CN", "🇨🇳", "简体中文"),
)

BY_CODE = {lang.code: lang for lang in LANGUAGES}

# users.language is String(10); the longest code here is 5 characters.
MAX_CODE_LENGTH = max(len(lang.code) for lang in LANGUAGES)


def get(code: str):
    """The Language for a code, or None."""
    return BY_CODE.get(code)


def button_label(lang: Language) -> str:
    """Flag then name, as the picker shows it."""
    return f"{lang.flag} {lang.name}"
