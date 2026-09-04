"""Whole-application wiring - would have caught the exact class of bug a
package split risks: a handler function or conversation-state constant that
bot.py references but that no longer exists (or moved) after a refactor.

Building the real Application object is what actually resolves every
`admin_conversations.X` / `admin_handlers.X` / ... reference bot.py makes,
at handler-registration time - not lazily, so a broken reference here
raises immediately instead of only failing the first time a user hits that
particular button.
"""

import re

import bot as bot_module


def test_build_application_succeeds():
    app = bot_module.build_application()
    # Sanity floor, not an exact count: a handler being added/removed is
    # expected over time, a handful disappearing because a module failed to
    # import silently is not.
    assert len(app.handlers[0]) > 50


def test_every_admin_conversations_reference_in_bot_py_resolves():
    """admin_conversations is a package (handlers/admin_conversations/) split
    into products/categories/store_settings/broadcast + a shared module -
    this is exactly the kind of reference that split could have silently
    dropped without every name being re-exported from __init__.py.
    """
    import handlers.admin_conversations as ac

    bot_src = open(bot_module.__file__).read()
    names = set(re.findall(r'admin_conversations\.([A-Za-z_][A-Za-z0-9_]*)', bot_src))
    assert names, "regex found nothing - bot.py's admin_conversations usage pattern changed"

    missing = [n for n in names if not hasattr(ac, n)]
    assert not missing, f"bot.py references admin_conversations.{missing} which no longer exists"


def test_building_the_application_is_quiet():
    """Twenty expected warnings per boot bury the one that matters.

    python-telegram-bot warns once per ConversationHandler that a
    CallbackQueryHandler is not tracked per message while per_message is
    False. That is the correct setting here - every one of these
    conversations mixes button taps with typed replies, and per_message=True
    only works for a conversation made of buttons alone - so the warning is
    expected and is filtered. Anything else must still come through.
    """
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        bot_module.apply_warning_filters()
        bot_module.build_application()

    unexpected = [str(w.message) for w in caught]
    assert not unexpected, "unexpected warnings at boot:\n  " + "\n  ".join(unexpected)
