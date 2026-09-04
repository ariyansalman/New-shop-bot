"""Telegram accepts ONE answer per callback query and drops the rest.

A handler that answers up front and then answers again with an alert
therefore shows the admin nothing at all: the button appears dead. That is
exactly how the Binance panel's Enable button failed - it refused every
press through an alert Telegram had already discarded.

The bug is invisible in review (both calls look fine on their own) and
invisible at runtime (no exception, no log), so it is worth a structural
check rather than one test per handler.
"""

import ast
import pathlib

HANDLERS = pathlib.Path(__file__).resolve().parent.parent / "handlers"


def _answer_calls(fn):
    """Every `.answer(...)` in one function, as (line, is_alert, has_text)."""
    calls = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "answer"):
            calls.append((
                node.lineno,
                any(kw.arg == "show_alert" for kw in node.keywords),
                bool(node.args),
            ))
    return calls


def test_no_handler_sends_an_alert_after_answering():
    offenders = []

    for path in sorted(HANDLERS.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            calls = _answer_calls(fn)
            bare = [c for c in calls if not c[1] and not c[2]]
            alerts = [c for c in calls if c[1]]
            if not bare or not alerts:
                continue
            if min(b[0] for b in bare) < max(a[0] for a in alerts):
                offenders.append(
                    f"{path.name}:{fn.name} - bare answer() on line "
                    f"{min(b[0] for b in bare)} silences the alert(s) on "
                    f"line(s) {sorted(a[0] for a in alerts)}"
                )

    assert not offenders, (
        "These handlers answer the callback and then try to alert, which "
        "Telegram discards - the button will look dead:\n  "
        + "\n  ".join(offenders)
    )
