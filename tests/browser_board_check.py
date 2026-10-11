"""Real-browser DOM verification for the task board (D04/D05).

Standalone: imports NO project package. It loads a page snapshot produced by
the locked interpreter (set BOARD_TEST_OUTPUT_DIR for both dump and check)
and drives real Chromium via
the machine-local playwright installation.

Usage: python tests/browser_board_check.py   (from any interpreter with playwright)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(os.environ.get("BOARD_TEST_OUTPUT_DIR") or Path(__file__).resolve().parent)
PAGE = HERE / "board_page.html"
STATES = HERE / "board_states.json"

CHECKS = []


def check(name, condition, detail=""):
    CHECKS.append((name, bool(condition), detail))
    print("{0} {1}{2}".format("PASS" if condition else "FAIL", name, (" :: " + detail) if detail and not condition else ""))


def main() -> int:
    from playwright.sync_api import sync_playwright

    states = json.loads(STATES.read_text(encoding="utf-8"))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(PAGE.as_uri())
        render = lambda state: page.evaluate("state => window.__taskBoardRender(state)", state)  # noqa: E731

        # D05: first client render recognizes SSR rows -> no duplicates.
        before = page.locator("tr[data-task]").count()
        render(states["v1"])
        after = page.locator("tr[data-task]").count()
        check("no_duplicate_rows_on_first_render", before == 1 and after == 1, "before={0} after={1}".format(before, after))

        # D05: typing marks dirty; same-version refresh keeps value AND focus.
        notes = page.locator('form[data-task="{0}"] input[name="notes"]'.format(states["tid"]))
        notes.click()
        notes.fill("正在输入的人工备注")
        render(states["v1"])
        value = notes.input_value()
        focused = page.evaluate("document.activeElement && document.activeElement.name")
        check("dirty_value_preserved", value == "正在输入的人工备注", "value={0!r}".format(value))
        check("focus_preserved_on_refresh", focused == "notes", "activeElement={0!r}".format(focused))

        # D04: a CLEAN (never-typed) form survives a version bump with an
        # operable form; the previous form was dirty, so reload for a clean one.
        page.reload()
        render(states["v2"])
        forms = page.locator("form[data-task]").count()
        version = page.locator("tr[data-task]").first.get_attribute("data-version")
        check("clean_form_survives_version_update", forms == 1 and version == "2", "forms={0} version={1}".format(forms, version))

        # Conflict: dirty form + version change keeps input, flags stale.
        notes2 = page.locator('form[data-task="{0}"] input[name="notes"]'.format(states["tid"]))
        notes2.click()
        page.evaluate(
            "sel => { const el = document.querySelector(sel); el.value = el.value + 'conflict draft';"
            " el.dispatchEvent(new Event('input', {bubbles: true})); }",
            'form[data-task="{0}"] input[name="notes"]'.format(states["tid"]),
        )
        render(states["v3"])
        kept = notes2.input_value()
        stale = page.locator("tr.stale-form").count()
        check("dirty_form_conflict_flagged_not_bound", "conflict draft" in kept and stale >= 1,
              "value={0!r} stale={1}".format(kept, stale))

        # STRICT focus retention through a REAL reorder (task moves first -> last).
        page.evaluate(
            "sel => { const el = document.querySelector(sel); window.__focusNode = el;"
            " el.focus(); el.setSelectionRange(4, 9); }",
            'form[data-task="{0}"] input[name="notes"]'.format(states["tid"]),
        )
        order_before = page.evaluate("Array.from(document.querySelectorAll('tr[data-task]')).map(r => r.getAttribute('data-task'))")
        render(states["reordered"])
        order_with_focus = page.evaluate("Array.from(document.querySelectorAll('tr[data-task]')).map(r => r.getAttribute('data-task'))")
        check("reorder_with_focus_defers_focused_group",
              order_with_focus[0] == states["tid"] and set(order_with_focus) == {states["tid"], "task_other_a", "task_other_b"},
              "order={0}".format(order_with_focus))
        strict_focus = page.evaluate(
            "document.activeElement === window.__focusNode"
            " && document.activeElement.selectionStart === 4 && document.activeElement.selectionEnd === 9"
        )
        value_kept = page.evaluate("window.__focusNode.value")
        check("focus_node_identity_and_selection_kept_through_reorder",
              strict_focus is True and value_kept == kept,
              "strict={0} value={1!r}".format(strict_focus, value_kept))
        # Once focus leaves the group, the next render completes the reorder.
        page.evaluate("document.activeElement.blur()")
        render(states["reordered"])
        order_final = page.evaluate("Array.from(document.querySelectorAll('tr[data-task]')).map(r => r.getAttribute('data-task'))")
        check("reorder_completes_after_blur",
              order_final[-1] == states["tid"] and order_final[:2] == ["task_other_a", "task_other_b"],
              "final={0}".format(order_final))

        browser.close()

    failed = [name for name, ok, _ in CHECKS if not ok]
    print("\n{0}/{1} checks passed".format(len(CHECKS) - len(failed), len(CHECKS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
