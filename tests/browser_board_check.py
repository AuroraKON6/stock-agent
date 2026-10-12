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

        # D06: task detail drawer - real row-click wiring through the fetch
        # path (network stubbed at window.fetch; the HTTP endpoint itself is
        # contract-tested against a live server elsewhere), drawer content,
        # persistence across refresh renders, Esc close, and form-click guard.
        page.reload()
        render(states["v1"])
        detail_fixture = {
            "task_id": states["tid"], "status": "awaiting_human", "is_replay": False,
            "title": "浏览器验收任务", "input_version": 1, "row_version": 1,
            "attempt_count": 1, "max_attempts": 2,
            "origin": {"kind": "event", "event_key": "event:sha256:ab", "supersedes_task_id": "task_old"},
            "research_question": "评估该公告的影响", "symbols": ["600519"],
            "evidence_delivery": {"delivered_quotes": ["600519"], "delivered_evidence": [],
                                   "dropped_quotes": ["000001:market_governance[stale]"], "dropped_evidence": []},
            "research_rounds": [{"round": 1, "actual_model": "deepseek-flash", "final": False,
                                  "intent_results": [{"action": "read_public_quote_snapshot", "status": "ok"}]}],
            "model_call": {"request_model": "deepseek-flash", "actual_model": "deepseek-flash",
                            "called": True, "usage": {"prompt_tokens": 10}},
            "output": {"summary": "阶段结论：行情已确认。"},
            "pending_human_questions": [{"question": "是否补充三季报原件？", "why_needed": "", "options": []}],
            "last_error": "",
            "scope_note": "personal_research_only_not_institutional",
        }
        page.evaluate(
            "fixture => { window.fetch = (url) => Promise.resolve({ok: true, json: () => Promise.resolve(fixture)}); }",
            detail_fixture)
        page.locator('tr[data-task="{0}"]'.format(states["tid"])).first.click()
        drawer = page.locator("#detail")
        check("detail_drawer_opens_on_row_click", drawer.is_visible())
        drawer_text = drawer.inner_text()
        check("detail_shows_rounds_model_question_lineage",
              "deepseek-flash" in drawer_text and "评估该公告的影响" in drawer_text
              and "round 1" in drawer_text and "supersedes" in drawer_text,
              "head={0!r}".format(drawer_text[:200]))
        check("detail_shows_delivery_and_pending_question",
              "600519" in drawer_text and "是否补充三季报原件" in drawer_text
              and "market_governance" in drawer_text,
              "delivery/pending facts missing")
        # The drawer is an independent panel: a state refresh must not close it.
        render(states["v2"])
        check("detail_drawer_survives_refresh_render", drawer.is_visible())
        page.keyboard.press("Escape")
        check("detail_drawer_esc_closes", not drawer.is_visible())
        # Clicking inside the feedback form must never open the drawer.
        page.reload()
        render(states["v1"])
        page.locator('form[data-task="{0}"] input[name="notes"]'.format(states["tid"])).click()
        check("form_click_does_not_open_detail", not page.locator("#detail").is_visible())

        # D10: workbench overview bar - fetched projection rendered as chips,
        # real fetch path (network stubbed at window.fetch like the drawer),
        # and state refresh renders never clear it.
        page.reload()
        render(states["v1"])
        overview_fixture = {
            "engine": {"event_intake_mode": "task"},
            "market": {"symbols": ["600519"], "quotes": {"600519": {"price": 1500.0}},
                        "quotes_missing": ["000001"]},
            "events": {"eligible_count": 3},
            "tasks": {"status_counts": {"queued": 1, "awaiting_human": 1},
                       "awaiting_human": [{"task_id": "t_x", "question": "补充材料？"}]},
            "budget": {"calls_today": 2, "limit": 4, "enabled": True},
        }
        page.evaluate(
            "fixture => { window.fetch = (url) => Promise.resolve({ok: true, json: () => Promise.resolve(fixture)});"
            " window.__taskBoardOverview(fixture); }",
            overview_fixture)
        bar = page.locator("#overview")
        check("overview_bar_visible", bar.is_visible())
        bar_text = bar.inner_text()
        check("overview_chips_show_engine_quotes_events",
              "task" in bar_text and "quotes" in bar_text and "events 24h" in bar_text
              and "missing" in bar_text,
              "text={0!r}".format(bar_text[:200]))
        check("overview_chips_show_queue_awaiting_budget",
              "queued" in bar_text and "awaiting human" in bar_text
              and "model calls today" in bar_text and "limit" in bar_text,
              "text={0!r}".format(bar_text[:200]))
        render(states["v2"])
        check("overview_bar_survives_refresh_render", bar.is_visible() and "task" in bar.inner_text())

        # D10 events section: deduped stories with revision badge, sources and
        # linked-task chips; clicking a chip opens the detail drawer (URL-aware
        # fetch stub so the detail request returns the detail fixture).
        page.reload()
        render(states["v1"])
        events_fixture = {
            "event_count": 2,
            "events": [
                {"cluster_key": "cluster::甲::600519", "title": "甲公司业绩预告更正",
                 "symbols": ["600519"], "sources": ["eastmoney", "sina"], "report_count": 3,
                 "versions": ["1", "2"], "revision": True,
                 "first_published": "2026-10-12T09:00:00+08:00",
                 "last_published": "2026-10-12T15:00:00+08:00",
                 "linked_tasks": [{"task_id": states["tid"], "status": "awaiting_human"}]},
                {"cluster_key": "cluster::乙::000001", "title": "乙公司常规公告",
                 "symbols": ["000001"], "sources": ["cninfo"], "report_count": 1,
                 "versions": ["1"], "revision": False,
                 "first_published": "2026-10-12T10:00:00+08:00",
                 "last_published": "2026-10-12T10:00:00+08:00",
                 "linked_tasks": []},
            ],
        }
        page.evaluate(
            "(payload) => { window.__eventsPayload = payload; window.__taskBoardEvents(payload); }",
            events_fixture)
        events_section = page.locator("#events")
        events_text = page.locator("#evlist").inner_text()
        check("events_section_renders_stories",
              "甲公司业绩预告更正" in events_text and "乙公司常规公告" in events_text,
              "text={0!r}".format(events_text[:200]))
        check("events_revision_badge_and_sources",
              "修订" in events_text and "eastmoney" in events_text and "sina" in events_text,
              "missing revision badge or sources")
        page.evaluate(
            "() => { window.fetch = (url) => Promise.resolve({ok: true, json: () =>"
            " Promise.resolve(String(url).indexOf('/tasks/') >= 0"
            " ? {task_id: '" + states["tid"] + "', status: 'awaiting_human'}"
            " : window.__eventsPayload)}); }")
        page.locator('[data-tasklink="{0}"]'.format(states["tid"])).first.click()
        drawer_after = page.locator("#detail")
        check("events_linked_task_chip_opens_detail",
              drawer_after.is_visible() and states["tid"] in drawer_after.inner_text())
        events_section.locator("summary").click()
        open_state = page.evaluate("document.getElementById('events').open")
        check("events_section_collapsible", open_state is False, "open={0!r}".format(open_state))

        # D10 todo section: awaiting questions with age badges and a
        # hand-off button that opens the detail drawer; blocked tasks with
        # their denial reason; summary count includes review-queue pendings.
        page.reload()
        render(states["v1"])
        todo_fixture = {
            "counts": {"awaiting_human": 1, "blocked": 1},
            "awaiting_human": [
                {"task_id": states["tid"], "title": "研究任务",
                 "question": "是否补充三季报原件？", "why_needed": "缺上下文",
                 "options": ["Q3", "年报"], "updated_at": "2026-10-12T15:00:00+08:00",
                 "age_minutes": 125},
            ],
            "blocked": [
                {"task_id": "task_blk", "title": "被阻断任务",
                 "error": "governance denied: internal classification",
                 "updated_at": "2026-10-12T14:00:00+08:00", "note": "CLI"},
            ],
            "review_queue": {"available": True, "pending_count": 3, "generated_at": "x"},
        }
        page.evaluate("payload => window.__taskBoardTodo(payload)", todo_fixture)
        todo_text = page.locator("#todolist").inner_text()
        check("todo_section_renders_question_age_blocked",
              "是否补充三季报原件" in todo_text and "2h" in todo_text
              and "被阻断任务" in todo_text and "governance denied" in todo_text,
              "text={0!r}".format(todo_text[:240]))
        check("todo_summary_counts_review_queue",
              "（5）" in page.locator("#todo summary").inner_text()
              and "复核队列" in todo_text and "pending 3" in todo_text,
              "summary={0!r}".format(page.locator("#todo summary").inner_text()))
        page.evaluate(
            "() => { window.fetch = (url) => Promise.resolve({ok: true, json: () =>"
            " Promise.resolve(String(url).indexOf('/tasks/') >= 0"
            " ? {task_id: '" + states["tid"] + "', status: 'awaiting_human'}"
            " : {})}); }")
        page.locator('#todolist [data-tasklink="{0}"]'.format(states["tid"])).first.click()
        drawer_todo = page.locator("#detail")
        check("todo_handoff_button_opens_detail",
              drawer_todo.is_visible() and states["tid"] in drawer_todo.inner_text())

        browser.close()

    failed = [name for name, ok, _ in CHECKS if not ok]
    print("\n{0}/{1} checks passed".format(len(CHECKS) - len(failed), len(CHECKS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
