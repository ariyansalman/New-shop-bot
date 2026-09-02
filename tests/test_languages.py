"""The ten languages, and the picker that chooses between them.

The checks that matter most are structural: a translation is easy to add
with a placeholder renamed or a key missing, and neither shows up until a
customer hits that exact screen in that exact language.
"""

import json
import pathlib
import re
import string

import pytest

from utils.i18n import t, SUPPORTED_LANGS, DEFAULT_LANG, _LOCALES_DIR
from utils.languages import LANGUAGES, BY_CODE, MAX_CODE_LENGTH, button_label
from utils.keyboards import create_language_keyboard, create_main_menu_keyboard

EXPECTED = ("en", "am", "ar", "bn", "fa", "hi", "pt-BR", "ru", "vi", "zh-CN")


def locale(code):
    path = pathlib.Path(_LOCALES_DIR) / f"{code}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def placeholders(template):
    """The {names} a format string expects."""
    return {name for _lit, name, _spec, _conv
            in string.Formatter().parse(template) if name}


# ------------------------------------------------------------- registry

def test_every_expected_language_is_offered():
    assert tuple(lang.code for lang in LANGUAGES) == EXPECTED


def test_english_is_first_because_everything_falls_back_to_it():
    assert LANGUAGES[0].code == DEFAULT_LANG


def test_codes_fit_the_database_column():
    """users.language is String(10); pt-BR and zh-CN are the long ones."""
    assert MAX_CODE_LENGTH <= 10


def test_each_language_is_named_in_its_own_script():
    """Someone who cannot read the current interface uses this screen."""
    assert BY_CODE["bn"].name == "বাংলা"
    assert BY_CODE["ru"].name == "Русский"
    assert BY_CODE["zh-CN"].name == "简体中文"
    assert BY_CODE["am"].name == "አማርኛ"


def test_every_entry_has_a_flag():
    for lang in LANGUAGES:
        assert lang.flag, lang.code
        assert button_label(lang).startswith(lang.flag)


# ------------------------------------------------------------- catalogue

@pytest.mark.parametrize("code", EXPECTED)
def test_a_locale_file_exists_and_loads(code):
    assert locale(code)


@pytest.mark.parametrize("code", EXPECTED)
def test_no_language_is_missing_a_key(code):
    """A missing key silently falls back to English on that one screen."""
    missing = set(locale(DEFAULT_LANG)) - set(locale(code))

    assert not missing, f"{code} is missing: {sorted(missing)}"


@pytest.mark.parametrize("code", EXPECTED)
def test_no_language_has_a_key_english_does_not(code):
    """A key only one language has is dead weight nothing can read."""
    extra = set(locale(code)) - set(locale(DEFAULT_LANG))

    assert not extra, f"{code} has unknown keys: {sorted(extra)}"


@pytest.mark.parametrize("code", EXPECTED)
def test_placeholders_match_english(code):
    """A renamed placeholder makes t() fall back mid-purchase."""
    english = locale(DEFAULT_LANG)
    for key, template in locale(code).items():
        assert placeholders(template) == placeholders(english[key]), \
            f"{code}/{key} placeholders drifted"


@pytest.mark.parametrize("code", EXPECTED)
def test_nothing_is_left_untranslated_by_accident(code):
    """Catches a locale file copied from English and not finished.

    Buttons and short labels legitimately coincide across languages, so
    this only looks at the long templates.
    """
    if code == DEFAULT_LANG:
        return
    english = locale(DEFAULT_LANG)
    long_keys = [k for k, v in english.items() if len(v) > 60]
    identical = [k for k in long_keys if locale(code)[k] == english[k]]

    assert not identical, f"{code} still English for: {identical}"


# ------------------------------------------------------------------ t()

@pytest.mark.parametrize("code", EXPECTED)
def test_the_wallet_line_renders_in_every_language(code):
    text = t('main_menu.wallet_balance', code, balance="$25.00")

    assert "$25.00" in text
    assert "{balance}" not in text


def test_an_unknown_language_falls_back_to_english():
    assert t('purchase.cancelled', 'klingon') == t('purchase.cancelled', 'en')


def test_an_unknown_key_returns_empty_rather_than_the_raw_key():
    assert t('no.such.key', 'en') == ""


def test_a_missing_argument_does_not_raise_inside_a_handler():
    """Better a degraded message than a traceback mid-purchase."""
    assert isinstance(t('main_menu.wallet_balance', 'en'), str)


# --------------------------------------------------------------- picker

def test_the_picker_is_two_columns_with_back_last():
    rows = create_language_keyboard().inline_keyboard

    assert all(len(row) <= 2 for row in rows)
    assert len(rows[-1]) == 1
    assert rows[-1][0].callback_data == "main_menu"


def test_the_picker_offers_every_language_once():
    data = [b.callback_data for row in create_language_keyboard().inline_keyboard
            for b in row if b.callback_data.startswith("set_lang_")]

    assert data == [f"set_lang_{code}" for code in EXPECTED]
    assert len(data) == len(set(data))


def test_the_picker_rows_follow_the_registry_order():
    """The screenshot's order: English first, then by code."""
    rows = create_language_keyboard().inline_keyboard
    assert [b.text for b in rows[0]] == ["🇬🇧 English", "🇪🇹 አማርኛ"]
    assert [b.text for b in rows[4]] == ["🇻🇳 Tiếng Việt", "🇨🇳 简体中文"]


def test_a_hyphenated_code_survives_the_callback_round_trip():
    """set_lang_pt-BR must not be parsed as "pt"."""
    for code in ("pt-BR", "zh-CN"):
        parsed = f"set_lang_{code}".split("_", 2)[2]
        assert parsed == code
        assert parsed in SUPPORTED_LANGS


def test_a_callback_data_stays_inside_telegrams_limit():
    """Telegram rejects callback_data over 64 bytes."""
    for row in create_language_keyboard().inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64


# ------------------------------------------------------------ main menu

@pytest.mark.parametrize("code", EXPECTED)
def test_the_main_menu_language_button_opens_the_picker(code):
    """It used to toggle between two languages; with ten there is no
    single "other one" to offer."""
    targets = [b.callback_data for row in
               create_main_menu_keyboard(code).inline_keyboard for b in row]

    assert "language" in targets
    assert not any(d.startswith("set_lang_") for d in targets)


@pytest.mark.parametrize("code", EXPECTED)
def test_the_main_menu_is_translated(code):
    labels = [b.text for row in create_main_menu_keyboard(code).inline_keyboard
              for b in row]

    assert labels == [t(key, code) for key in (
        'main_menu.button.products', 'main_menu.button.topup',
        'main_menu.button.order_history', 'main_menu.button.availability',
        'main_menu.button.support', 'main_menu.button.language')]


def test_locale_files_have_no_stray_ascii_placeholders():
    """A literal "{" that is not a real placeholder breaks .format()."""
    for code in EXPECTED:
        for key, template in locale(code).items():
            for brace in re.findall(r"\{[^}]*\}", template):
                assert brace[1:-1].isidentifier(), f"{code}/{key}: {brace}"
