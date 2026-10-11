from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from stock_agent.models import now_iso
from stock_agent.research_runtime import DEFAULT_COMPLIANCE_CHECKS, sync_research_runtime
from stock_agent.review_workbench import (
    _is_loopback_host,
    _is_loopback_origin,
    create_workbench_server,
    load_runtime_workbench_state,
    render_runtime_workbench_html,
    submit_runtime_workbench_action,
)


def _case(priority="P1", source_types=None, with_analysis=True):
    analysis = {}
    if with_analysis:
        analysis = {
            "hypotheses": [
                {
                    "name": "base",
                    "thesis": "订单对未来收入形成支持，但交付节奏仍需跟踪。",
                    "probability": 0.7,
                    "evidence_for": ["official filing"],
                    "evidence_against": ["delivery uncertainty"],
                }
            ],
            "evidence_gaps": [],
            "counter_questions": ["订单是否可撤销？"],
            "human_decision_needed": True,
        }
    return {
        "case_id": "case001",
        "title": "测试公司重大合同",
        "entity": "688888",
        "topic": "AI服务器",
        "catalyst_path": "filing:major_contract",
        "status": "active",
        "top_priority": priority,
        "source_types": source_types or ["filing", "price"],
        "signal_ids": ["sig001"],
        "signal_count": 1,
        "latest_signal_at": now_iso(),
        "case_analysis": analysis,
    }


def _signal_dict(primary=True, with_roles=True):
    roles = []
    if with_roles:
        roles = [
            {"role_id": "fundamental_analyst", "thesis": "contract supports revenue"},
            {"role_id": "market_analyst", "thesis": "market reaction is moderate"},
            {"role_id": "risk_reviewer", "risk_points": ["delivery risk"]},
        ]
    return {
        "signal_id": "sig001",
        "source": "eastmoney_filing" if primary else "manual",
        "source_type": "filing" if primary else "news",
        "title": "测试公告",
        "summary": "重大合同",
        "url": "https://example.test/official.pdf" if primary else "https://example.test/news",
        "priority": "P1",
        "payload": {
            "filing_evidence": {"status": "ok" if primary else "unknown"},
        },
        "evidence_chain": {
            "unresolved_refs": [],
            "evidence_gap_tasks": [],
            "quality": {"status": "OK", "score": 1.0},
        },
        "llm_analysis": {"key_thesis": "合同带来收入增量", "risk_points": ["交付风险"]},
        "role_analyses": roles,
        "role_synthesis": {"conflict_level": "medium", "key_disagreement": "交付节奏"},
    }


def _config(root=None):
    config = {
        "research_runtime": {
            "enabled": True,
            "auto_sync": True,
            "retention_years": 5,
            "require_primary_source_for": ["P0", "P1"],
            "required_compliance_checks": list(DEFAULT_COMPLIANCE_CHECKS),
            "external_publication_enabled": False,
        }
    }
    if root is not None:
        config["storage"] = {"sqlite_path": str(root / "data" / "signals.sqlite3")}
    return config


def _patch_paths(root):
    stack = ExitStack()
    for name in [
        "stock_agent.research_runtime.project_path",
        "stock_agent.dashboard.project_path",
        "stock_agent.alerts.project_path",
        "stock_agent.review_workbench.project_path",
        "stock_agent.source_remediation.project_path",
        "stock_agent.review.project_path",
        "stock_agent.review_priority.project_path",
        "stock_agent.brokerage_architecture.project_path",
    ]:
        stack.enter_context(patch(name, side_effect=lambda value, r=root: r / value))
    return stack


def _seed(root):
    with _patch_paths(root):
        sync_research_runtime(_config(), cases=[_case()], signals=[_signal_dict()])


def _submit(root, form):
    with _patch_paths(root):
        return submit_runtime_workbench_action(form, config=_config(root))


class LoopbackParsingTests(unittest.TestCase):
    def test_host_rejects_malformed_and_suffix_authorities(self):
        for bad in [
            "[::1].evil",
            "[::1",
            "[::1]extra",
            "[::1]:",
            "[::1]:0",
            "[::1]:99999",
            "::1.0",
            "::1:8767",
            "localhost.evil.com",
            "localhost:99999",
            "localhost:",
            "127.0.0.1.evil",
            "127.0.0.1:99999",
            "127.0.0.1:",
            "evil@localhost",
            "localhost/path",
            "localhost?query",
            "localhost#frag",
            "localhost:9999:evil",
            "127.0.0.1:8767:evil",
            "0.0.0.0",
            "::",
            "example.com",
            "",
        ]:
            self.assertFalse(_is_loopback_host(bad))

    def test_host_accepts_exact_loopback_authorities(self):
        for good in [
            "127.0.0.1",
            "127.0.0.1:8767",
            "localhost",
            "localhost:8767",
            "::1",
            "[::1]",
            "[::1]:8767",
            "[::1]:65535",
            "127.0.0.1:65535",
            "LOCALHOST",
        ]:
            self.assertTrue(_is_loopback_host(good))

    def test_origin_requires_exact_same_origin(self):
        self.assertTrue(_is_loopback_origin("http://127.0.0.1:8767", "127.0.0.1:8767"))
        self.assertTrue(_is_loopback_origin("http://localhost:80", "localhost:80"))
        # Effective-port equivalence: an implicit default matches an explicit one.
        self.assertTrue(_is_loopback_origin("http://127.0.0.1:80", "127.0.0.1"))
        self.assertTrue(_is_loopback_origin("http://127.0.0.1", "127.0.0.1"))
        self.assertTrue(_is_loopback_origin("http://[::1]:8767", "[::1]:8767"))
        # https is not acceptable for this plain HTTP server.
        self.assertFalse(_is_loopback_origin("https://127.0.0.1:8767", "127.0.0.1:8767"))
        # Loopback alias change: same port, different hostname spelling.
        self.assertFalse(_is_loopback_origin("http://localhost:8767", "127.0.0.1:8767"))
        self.assertFalse(_is_loopback_origin("http://127.0.0.1:8767", "localhost:8767"))
        # Mismatched port.
        self.assertFalse(_is_loopback_origin("http://127.0.0.1:9999", "127.0.0.1:8767"))
        self.assertFalse(_is_loopback_origin("http://127.0.0.1", "127.0.0.1:8767"))
        # Credentials are never acceptable.
        self.assertFalse(_is_loopback_origin("http://user@127.0.0.1:8767", "127.0.0.1:8767"))
        self.assertFalse(_is_loopback_origin("http://user:pass@127.0.0.1:8767", "127.0.0.1:8767"))
        # Paths, query strings, and fragments are not origins.
        self.assertFalse(_is_loopback_origin("http://127.0.0.1:8767/path", "127.0.0.1:8767"))
        self.assertFalse(_is_loopback_origin("http://127.0.0.1:8767?x=1", "127.0.0.1:8767"))
        self.assertFalse(_is_loopback_origin("http://127.0.0.1:8767#frag", "127.0.0.1:8767"))
        # Malformed IPv6 and suffix hosts.
        self.assertFalse(_is_loopback_origin("http://[::1].evil", "127.0.0.1:8767"))
        self.assertFalse(_is_loopback_origin("http://127.0.0.1.evil:8767", "127.0.0.1:8767"))


class RuntimeWorkbenchStateTests(unittest.TestCase):
    def test_runtime_state_filters_by_stage_and_priority(self):
        case_a = _case(priority="P1")
        case_b = _case(priority="P3", source_types=["news"], with_analysis=False)
        case_b["case_id"] = "case002"
        case_b["signal_ids"] = ["sig002"]
        signal_b = _signal_dict(primary=False, with_roles=False)
        signal_b["signal_id"] = "sig002"
        signal_b["url"] = ""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                sync_research_runtime(_config(), cases=[case_a, case_b], signals=[_signal_dict(), signal_b])
                state = load_runtime_workbench_state(stage="evidence_collection", priority="P3", config=_config(root))

        self.assertEqual(state["stage"], "evidence_collection")
        self.assertEqual(state["priority"], "P3")
        self.assertEqual([item["case_id"] for item in state["filtered_cases"]], ["case002"])

    def test_selected_case_is_independent_of_filters(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                sync_research_runtime(_config(), cases=[_case()], signals=[_signal_dict()])
                state = load_runtime_workbench_state(stage="intake", priority="P0", case_id="case001", config=_config(root))

        self.assertEqual(state["filtered_cases"], [])
        self.assertEqual(state["selected_case"]["case_id"], "case001")
        # Unknown / stale case id must not crash and leaves no selection.
        with tempfile.TemporaryDirectory() as tmpdir2:
            root2 = Path(tmpdir2)
            with _patch_paths(root2):
                sync_research_runtime(_config(), cases=[_case()], signals=[_signal_dict()])
                stale = load_runtime_workbench_state(case_id="does-not-exist", config=_config(root2))
        self.assertIsNone(stale["selected_case"])

    def test_runtime_render_contains_expected_sections(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                sync_research_runtime(_config(), cases=[_case()], signals=[_signal_dict()])
                html = render_runtime_workbench_html(case_id="case001", config=_config(root), csrf_token="tok123")

        self.assertIn("Research Cases", html)
        self.assertIn("Remediation", html)
        self.assertIn("Case Queue", html)
        self.assertIn("Gates", html)
        self.assertIn("Workstreams", html)
        self.assertIn("Assignments", html)
        self.assertIn("Workpapers", html)
        self.assertIn("Approvals", html)
        self.assertIn("Action History", html)
        self.assertIn("Controlled Actions", html)
        self.assertIn("case001", html)
        self.assertIn('name="csrf_token"', html)
        self.assertIn("tok123", html)
        # No oversized marketing copy; footer states the research-only boundary.
        self.assertIn("no automatic trading", html.lower())

    def test_degraded_runtime_highlights_preserved_cases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                sync_research_runtime(_config(), cases=[_case()], signals=[_signal_dict()])
                sync_research_runtime(_config(), cases=[], signals=[])
                html = render_runtime_workbench_html(config=_config(root))

        self.assertIn("Runtime degraded", html)
        self.assertIn("case001", html)
        self.assertIn("preserved", html.lower())

    def test_runtime_html_escapes_user_data(self):
        case = _case()
        case["title"] = "<script>alert('x')</script>"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                sync_research_runtime(_config(), cases=[case], signals=[_signal_dict()])
                html = render_runtime_workbench_html(case_id="case001", config=_config(root))

        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)


class RuntimeActionSubmitTests(unittest.TestCase):
    def test_requires_explicit_actor_and_role(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed(root)
            with self.assertRaisesRegex(ValueError, "actor and actor_role are required"):
                _submit(root, {"action": "assign_lead", "case_id": "case001", "assignee": "analyst-a"})
            with self.assertRaisesRegex(ValueError, "actor and actor_role are required"):
                _submit(root, {"action": "assign_lead", "case_id": "case001", "actor": "supervisor-a", "assignee": "analyst-a"})
            with self.assertRaisesRegex(ValueError, "case_id is required"):
                _submit(root, {"action": "assign_lead", "actor": "supervisor-a", "actor_role": "research_supervisor", "assignee": "analyst-a"})

    def test_rejects_unknown_and_trading_actions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed(root)
            for bad in ["place_order", "buy", "sell", "rebalance_position", "auto_trade", "not_an_action"]:
                with self.assertRaises(ValueError):
                    _submit(root, {"action": bad, "case_id": "case001", "actor": "a", "actor_role": "b"})

    def test_maps_assignment_action_and_parses_assignee(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed(root)
            result = _submit(root, {
                "action": "assign_lead",
                "case_id": "case001",
                "actor": "supervisor-a",
                "actor_role": "research_supervisor",
                "assignee": "analyst-a",
            })

        self.assertEqual(result["record"]["action"], "assign_lead")
        self.assertEqual(result["record"]["actor"], "supervisor-a")
        self.assertEqual(result["record"]["actor_role"], "research_supervisor")
        self.assertEqual(result["case"]["assignments"]["lead_analyst"], "analyst-a")

    def test_parses_references_and_checks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed(root)
            _submit(root, {
                "action": "assign_quality_reviewer",
                "case_id": "case001",
                "actor": "supervisor-a",
                "actor_role": "research_supervisor",
                "assignee": "reviewer-a",
            })
            result = _submit(root, {
                "action": "quality_approve",
                "case_id": "case001",
                "actor": "reviewer-a",
                "actor_role": "quality_reviewer",
                "notes": "checked",
                "reference": ["research_note:a", "source_document:b"],
                "references": "meeting_note:c\nresearch_note:d",
                "check": ["public_information_only"],
            })

        self.assertEqual(result["record"]["references"], ["research_note:a", "source_document:b", "meeting_note:c", "research_note:d"])
        self.assertEqual(result["record"]["checks"], ["public_information_only"])

    def test_refreshes_dashboard_and_notifications_after_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed(root)
            with patch("stock_agent.review_workbench.generate_dashboard") as mock_dash, patch(
                "stock_agent.review_workbench.generate_notifications"
            ) as mock_notify:
                result = _submit(root, {
                    "action": "assign_lead",
                    "case_id": "case001",
                    "actor": "supervisor-a",
                    "actor_role": "research_supervisor",
                    "assignee": "analyst-a",
                })

        self.assertEqual(result["record"]["action"], "assign_lead")
        mock_dash.assert_called_once()
        mock_notify.assert_called_once()

    def test_refresh_failures_are_sanitized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed(root)
            with patch(
                "stock_agent.review_workbench.generate_dashboard",
                side_effect=RuntimeError("boom C:\\secret\\vault"),
            ), patch(
                "stock_agent.review_workbench.generate_notifications",
                side_effect=PermissionError("denied http://internal.host/private-token"),
            ):
                result = _submit(root, {
                    "action": "assign_lead",
                    "case_id": "case001",
                    "actor": "supervisor-a",
                    "actor_role": "research_supervisor",
                    "assignee": "analyst-a",
                })

        # The recorded action stays successful even though the best-effort
        # dashboard/notification refreshes failed.
        self.assertEqual(result["record"]["action"], "assign_lead")
        self.assertEqual(result["case"]["assignments"]["lead_analyst"], "analyst-a")
        self.assertIn("dashboard refresh failed (RuntimeError)", result["refresh_error"])
        self.assertIn("notification refresh failed (PermissionError)", result["refresh_error"])
        # Raw exception text — paths, URLs, headers, and secrets — must not leak.
        self.assertNotIn("secret", result["refresh_error"])
        self.assertNotIn("vault", result["refresh_error"])
        self.assertNotIn("internal", result["refresh_error"])
        self.assertNotIn("http://", result["refresh_error"])
        self.assertNotIn("C:\\", result["refresh_error"])

    def test_rejected_action_raises_before_any_refresh(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed(root)
            with patch("stock_agent.review_workbench.generate_dashboard") as mock_dash, patch(
                "stock_agent.review_workbench.generate_notifications"
            ) as mock_notify:
                with self.assertRaises(ValueError):
                    _submit(root, {
                        "action": "place_order",
                        "case_id": "case001",
                        "actor": "supervisor-a",
                        "actor_role": "research_supervisor",
                    })
            mock_dash.assert_not_called()
            mock_notify.assert_not_called()

    def test_llm_actor_cannot_pass_quality_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed(root)
            _submit(root, {
                "action": "assign_quality_reviewer",
                "case_id": "case001",
                "actor": "supervisor-a",
                "actor_role": "research_supervisor",
                "assignee": "reviewer-a",
            })
            with self.assertRaisesRegex(ValueError, "cannot pass protected human gates"):
                _submit(root, {
                    "action": "quality_approve",
                    "case_id": "case001",
                    "actor": "llm-reviewer",
                    "actor_role": "quality_reviewer",
                    "notes": "auto approval",
                })

    def test_role_separation_enforced(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed(root)
            _submit(root, {
                "action": "assign_lead",
                "case_id": "case001",
                "actor": "supervisor-a",
                "actor_role": "research_supervisor",
                "assignee": "same-person",
            })
            with self.assertRaisesRegex(ValueError, "must be separated"):
                _submit(root, {
                    "action": "assign_quality_reviewer",
                    "case_id": "case001",
                    "actor": "supervisor-a",
                    "actor_role": "research_supervisor",
                    "assignee": "same-person",
                })

    def test_compliance_clear_requires_every_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed(root)
            for action, assignee in [
                ("assign_lead", "analyst-a"),
                ("assign_quality_reviewer", "reviewer-a"),
                ("assign_compliance_officer", "compliance-a"),
                ("assign_approver", "supervisor-a"),
            ]:
                _submit(root, {
                    "action": action,
                    "case_id": "case001",
                    "actor": "supervisor-a",
                    "actor_role": "research_supervisor",
                    "assignee": assignee,
                })
            _submit(root, {
                "action": "quality_approve",
                "case_id": "case001",
                "actor": "reviewer-a",
                "actor_role": "quality_reviewer",
                "notes": "checked",
            })
            with self.assertRaisesRegex(ValueError, "Missing required compliance checks"):
                _submit(root, {
                    "action": "compliance_clear",
                    "case_id": "case001",
                    "actor": "compliance-a",
                    "actor_role": "compliance_officer",
                    "checks": ["public_information_only"],
                })

    def test_valid_human_sequence_advances_gates_without_trading_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed(root)
            for action, assignee in [
                ("assign_lead", "analyst-a"),
                ("assign_quality_reviewer", "reviewer-a"),
                ("assign_compliance_officer", "compliance-a"),
                ("assign_approver", "supervisor-a"),
            ]:
                _submit(root, {
                    "action": action,
                    "case_id": "case001",
                    "actor": "supervisor-a",
                    "actor_role": "research_supervisor",
                    "assignee": assignee,
                })
            quality = _submit(root, {
                "action": "quality_approve",
                "case_id": "case001",
                "actor": "reviewer-a",
                "actor_role": "quality_reviewer",
                "notes": "workpaper checked",
            })
            self.assertEqual(quality["case"]["stage"], "compliance_review")
            compliance = _submit(root, {
                "action": "compliance_clear",
                "case_id": "case001",
                "actor": "compliance-a",
                "actor_role": "compliance_officer",
                "notes": "checks complete",
                "checks": list(DEFAULT_COMPLIANCE_CHECKS),
            })
            self.assertEqual(compliance["case"]["stage"], "human_approval")
            published = _submit(root, {
                "action": "publication_approve",
                "case_id": "case001",
                "actor": "supervisor-a",
                "actor_role": "research_supervisor",
                "notes": "internal release",
            })
            self.assertEqual(published["case"]["stage"], "published")
            monitored = _submit(root, {
                "action": "start_monitoring",
                "case_id": "case001",
                "actor": "supervisor-a",
                "actor_role": "research_supervisor",
                "notes": "track delivery",
            })

        final_case = monitored["case"]
        self.assertEqual(final_case["stage"], "monitoring")
        self.assertFalse(final_case["auto_publish"])
        self.assertFalse(final_case["external_publication_allowed"])
        trading_tokens = ("buy", "sell", "order", "rebalance", "trade", "position")
        actions = [str(item.get("action") or "") for item in final_case.get("action_history", [])]
        self.assertFalse(any(any(token in action.lower() for token in trading_tokens) for action in actions))


class StageAwareFormTests(unittest.TestCase):
    def _advance_to_human_approval(self, root):
        for action, assignee in [
            ("assign_lead", "analyst-a"),
            ("assign_quality_reviewer", "reviewer-a"),
            ("assign_compliance_officer", "compliance-a"),
            ("assign_approver", "supervisor-a"),
        ]:
            _submit(root, {
                "action": action,
                "case_id": "case001",
                "actor": "supervisor-a",
                "actor_role": "research_supervisor",
                "assignee": assignee,
            })
        _submit(root, {
            "action": "quality_approve",
            "case_id": "case001",
            "actor": "reviewer-a",
            "actor_role": "quality_reviewer",
            "notes": "workpaper checked",
        })
        _submit(root, {
            "action": "compliance_clear",
            "case_id": "case001",
            "actor": "compliance-a",
            "actor_role": "compliance_officer",
            "notes": "checks complete",
            "checks": list(DEFAULT_COMPLIANCE_CHECKS),
        })

    def _render(self, root):
        with _patch_paths(root):
            return render_runtime_workbench_html(case_id="case001", config=_config(root), csrf_token="tok")

    def _assert_buttons(self, html, shown, hidden):
        for value in shown:
            self.assertIn('name="action" value="{0}"'.format(value), html)
        for value in hidden:
            self.assertNotIn('name="action" value="{0}"'.format(value), html)

    def test_protected_and_terminal_forms_are_stage_aware(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed(root)
            self._advance_to_human_approval(root)

            html = self._render(root)
            self._assert_buttons(
                html,
                ["publication_approve", "publication_reject"],
                ["start_monitoring", "close_case", "reopen_case"],
            )

            _submit(root, {
                "action": "publication_approve",
                "case_id": "case001",
                "actor": "supervisor-a",
                "actor_role": "research_supervisor",
                "notes": "internal release",
            })
            html = self._render(root)
            self._assert_buttons(
                html,
                ["start_monitoring"],
                ["publication_approve", "publication_reject", "close_case", "reopen_case"],
            )

            _submit(root, {
                "action": "start_monitoring",
                "case_id": "case001",
                "actor": "supervisor-a",
                "actor_role": "research_supervisor",
                "notes": "track delivery",
            })
            html = self._render(root)
            self._assert_buttons(
                html,
                ["close_case"],
                ["publication_approve", "publication_reject", "start_monitoring", "reopen_case"],
            )

            _submit(root, {
                "action": "close_case",
                "case_id": "case001",
                "actor": "supervisor-a",
                "actor_role": "research_supervisor",
                "notes": "thesis resolved",
            })
            html = self._render(root)
            self._assert_buttons(
                html,
                ["reopen_case"],
                ["publication_approve", "publication_reject", "start_monitoring", "close_case"],
            )


class WorkbenchHttpTests(unittest.TestCase):
    def _start_server(self, root):
        server = create_workbench_server(host="127.0.0.1", port=0, config=_config(root))
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        return server, thread, port

    def _stop_server(self, server, thread):
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    def _http(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, dict(resp.getheaders()), data
        finally:
            conn.close()

    def _post(self, port, path, form, extra_headers=None, token=None):
        payload = dict(form)
        if token is not None:
            payload["csrf_token"] = token
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if extra_headers:
            headers.update(extra_headers)
        return self._http(port, "POST", path, body=urllib.parse.urlencode(payload), headers=headers)

    def test_health_endpoint_and_security_headers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                _seed(root)
                server, thread, port = self._start_server(root)
                try:
                    status, headers, body = self._http(port, "GET", "/health")
                finally:
                    self._stop_server(server, thread)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["status"], "ok")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        self.assertIn("default-src 'none'", headers.get("Content-Security-Policy", ""))

    def test_unknown_route_returns_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                _seed(root)
                server, thread, port = self._start_server(root)
                try:
                    status, _, _ = self._http(port, "GET", "/not-a-route")
                    self.assertEqual(status, 404)
                    status, _, _ = self._post(port, "/not-a-route", {"action": "x"}, token="ignored")
                    self.assertEqual(status, 404)
                finally:
                    self._stop_server(server, thread)

    def test_unknown_route_post_consumes_body_repeatedly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                _seed(root)
                server, thread, port = self._start_server(root)
                try:
                    # Each request carries a nonempty declared body that the
                    # handler must consume before sending the 404. Closing the
                    # connection with the body unread lets Windows reset the
                    # socket (WinError 10054) before the client reads the
                    # response, so repeat the request to prove the race is gone.
                    for _ in range(25):
                        status, _, _ = self._post(
                            port, "/not-a-route",
                            {"action": "x", "csrf_token": server.csrf_token},
                        )
                        self.assertEqual(status, 404)
                finally:
                    self._stop_server(server, thread)

    def test_non_loopback_bind_rejected(self):
        for host in ["0.0.0.0", "::", "192.168.1.50", "example.com"]:
            with self.assertRaises(ValueError):
                create_workbench_server(host=host, port=0)

    def test_post_without_csrf_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                _seed(root)
                server, thread, port = self._start_server(root)
                try:
                    status, _, _ = self._post(port, "/runtime-action", {"action": "assign_lead", "case_id": "case001"})
                    self.assertEqual(status, 400)
                finally:
                    self._stop_server(server, thread)

    def test_post_with_invalid_csrf_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                _seed(root)
                server, thread, port = self._start_server(root)
                try:
                    status, _, _ = self._post(port, "/runtime-action", {"action": "assign_lead", "case_id": "case001"}, token="wrong-token")
                    self.assertEqual(status, 400)
                finally:
                    self._stop_server(server, thread)

    def test_post_with_invalid_host_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                _seed(root)
                server, thread, port = self._start_server(root)
                try:
                    # Nonempty declared body; it must be consumed before the 400
                    # is sent, or the connection close can reset on Windows.
                    for _ in range(10):
                        status, _, _ = self._post(
                            port, "/runtime-action",
                            {"action": "assign_lead", "case_id": "case001", "actor": "a", "actor_role": "b", "assignee": "c"},
                            extra_headers={"Host": "evil.example"},
                            token=server.csrf_token,
                        )
                        self.assertEqual(status, 400)
                finally:
                    self._stop_server(server, thread)

    def test_post_with_invalid_origin_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                _seed(root)
                server, thread, port = self._start_server(root)
                try:
                    # Nonempty declared body; same Windows reset race as the
                    # invalid-host and wrong-content-type cases.
                    for _ in range(10):
                        status, _, _ = self._post(
                            port, "/runtime-action",
                            {"action": "assign_lead", "case_id": "case001", "actor": "a", "actor_role": "b", "assignee": "c"},
                            extra_headers={"Origin": "http://evil.example"},
                            token=server.csrf_token,
                        )
                        self.assertEqual(status, 400)
                finally:
                    self._stop_server(server, thread)

    def test_post_rejects_suffix_and_malformed_host(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                _seed(root)
                server, thread, port = self._start_server(root)
                try:
                    for bad_host in [
                        "127.0.0.1.evil:8767",
                        "[::1].evil",
                        "localhost.evil.com:8767",
                        "localhost:9999:evil",
                        "evil@localhost:8767",
                        "localhost/path",
                    ]:
                        status, _, _ = self._post(
                            port, "/runtime-action",
                            {"action": "assign_lead", "case_id": "case001"},
                            extra_headers={"Host": bad_host},
                            token=server.csrf_token,
                        )
                        self.assertEqual(status, 400)
                finally:
                    self._stop_server(server, thread)

    def test_post_origin_must_match_request_host_exactly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                _seed(root)
                server, thread, port = self._start_server(root)
                try:
                    def _origin_status(origin):
                        return self._post(
                            port, "/runtime-action",
                            {
                                "action": "assign_lead",
                                "case_id": "case001",
                                "actor": "supervisor-a",
                                "actor_role": "research_supervisor",
                                "assignee": "analyst-a",
                            },
                            extra_headers={"Origin": origin},
                            token=server.csrf_token,
                        )[0]

                    host = "127.0.0.1:{0}".format(port)
                    # Same-origin success.
                    self.assertEqual(_origin_status("http://{0}".format(host)), 303)
                    # Loopback alias change and mismatched port are rejected.
                    self.assertEqual(_origin_status("http://localhost:{0}".format(port)), 400)
                    self.assertEqual(_origin_status("http://127.0.0.1:9999"), 400)
                    # https is not acceptable for this plain HTTP server.
                    self.assertEqual(_origin_status("https://{0}".format(host)), 400)
                    # Credentials and paths are never origins.
                    self.assertEqual(_origin_status("http://user@{0}".format(host)), 400)
                    self.assertEqual(_origin_status("http://{0}/path".format(host)), 400)
                    # Suffix host and malformed IPv6.
                    self.assertEqual(_origin_status("http://127.0.0.1.evil:{0}".format(port)), 400)
                    self.assertEqual(_origin_status("http://[::1].evil"), 400)
                finally:
                    self._stop_server(server, thread)

    def test_ipv6_loopback_server_when_available(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                _seed(root)
            try:
                server = create_workbench_server(host="::1", port=0, config=_config(root))
            except OSError:
                self.skipTest("IPv6 loopback (::1) is not available on this host")
            try:
                self.assertEqual(server.address_family, socket.AF_INET6)
                self.assertEqual(server.server_address[0], "::1")
                port = server.server_address[1]
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
                thread.start()
                try:
                    conn = http.client.HTTPConnection("::1", port, timeout=10)
                    try:
                        conn.request("GET", "/health")
                        resp = conn.getresponse()
                        body = resp.read()
                        self.assertEqual(resp.status, 200)
                        self.assertEqual(json.loads(body.decode("utf-8"))["status"], "ok")
                    finally:
                        conn.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
            finally:
                server.server_close()

    def test_post_with_wrong_content_type_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                _seed(root)
                server, thread, port = self._start_server(root)
                try:
                    # Each request carries a nonempty declared body that the
                    # handler must consume before sending the 400. Closing the
                    # connection with the body unread lets Windows reset the
                    # socket (WinError 10054) before the client reads the
                    # response, so repeat the request to prove the race is gone.
                    for _ in range(25):
                        status, _, _ = self._http(
                            port, "POST", "/runtime-action",
                            body=urllib.parse.urlencode({"action": "assign_lead", "csrf_token": server.csrf_token}),
                            headers={"Content-Type": "text/plain"},
                        )
                        self.assertEqual(status, 400)
                finally:
                    self._stop_server(server, thread)

    def test_post_with_oversized_body_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                _seed(root)
                server, thread, port = self._start_server(root)
                try:
                    # Declare a Content-Length beyond the 64 KiB limit; the
                    # handler must reject on the header before reading the body.
                    body = urllib.parse.urlencode({"csrf_token": server.csrf_token, "notes": "small"})
                    status, _, _ = self._http(
                        port, "POST", "/runtime-action",
                        body=body,
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Content-Length": "70000",
                        },
                    )
                    self.assertEqual(status, 400)
                finally:
                    self._stop_server(server, thread)

    def test_remediation_post_requires_csrf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                _seed(root)
                server, thread, port = self._start_server(root)
                try:
                    status, _, _ = self._post(port, "/action", {"action": "defer_remediation", "task_id": "x"})
                    self.assertEqual(status, 400)
                finally:
                    self._stop_server(server, thread)

    def test_successful_runtime_post_redirects(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                _seed(root)
                server, thread, port = self._start_server(root)
                try:
                    status, headers, _ = self._post(
                        port, "/runtime-action",
                        {
                            "action": "assign_lead",
                            "case_id": "case001",
                            "actor": "supervisor-a",
                            "actor_role": "research_supervisor",
                            "assignee": "analyst-a",
                        },
                        token=server.csrf_token,
                    )
                    location = headers.get("Location", "")
                    persisted = json.loads((root / "data" / "research_runtime" / "runtime.json").read_text(encoding="utf-8"))
                finally:
                    self._stop_server(server, thread)

        self.assertEqual(status, 303)
        self.assertIn("view=runtime", location)
        self.assertIn("case_id=case001", location)
        self.assertEqual(persisted["cases"][0]["assignments"]["lead_analyst"], "analyst-a")

    def test_validation_error_returns_400_without_stack_trace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                _seed(root)
                server, thread, port = self._start_server(root)
                try:
                    status, _, body = self._post(
                        port, "/runtime-action",
                        {"action": "assign_lead", "case_id": "case001"},
                        token=server.csrf_token,
                    )
                finally:
                    self._stop_server(server, thread)

        self.assertEqual(status, 400)
        content = body.decode("utf-8", errors="replace")
        self.assertNotIn("Traceback", content)
        self.assertNotIn('File "', content)

    def test_runtime_page_embeds_csrf_token_and_filters(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                _seed(root)
                server, thread, port = self._start_server(root)
                try:
                    status, headers, body = self._http(
                        port, "GET",
                        "/?view=runtime&stage=quality_review&priority=P1&case_id=case001",
                    )
                finally:
                    self._stop_server(server, thread)

        self.assertEqual(status, 200)
        content = body.decode("utf-8")
        self.assertIn("Research Cases", content)
        self.assertIn("case001", content)
        self.assertIn(server.csrf_token, content)
        self.assertIn('value="quality_review" selected', content)
        self.assertIn('value="P1" selected', content)


class WorkbenchBodyReadHardeningTests(unittest.TestCase):
    """Raw-socket tests for the bounded body-read lifecycle.

    A declared body larger than what the peer sends must never escape do_POST
    as an OSError traceback: a stalled partial body yields a sanitized 408 and
    an early EOF yields a sanitized 400 truncated-body response. All writes go
    to a throwaway tempdir via _patch_paths, and these error paths never reach
    the action submit layer, so nothing is recorded on disk.
    """

    def _start_server(self, root):
        server = create_workbench_server(host="127.0.0.1", port=0, config=_config(root))
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        return server, thread, port

    def _stop_server(self, server, thread):
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    def _raw_post(self, port, declared_length, body, stall=False):
        """Send a raw POST whose body is shorter than the declared length.

        With ``stall=False`` the write side is shut down so the server sees an
        early EOF (truncated body). With ``stall=True`` the socket stays open
        and the server must give up on the read timeout (stalled body).
        Returns ``(status, response_bytes)``.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        try:
            sock.connect(("127.0.0.1", port))
            head = (
                "POST /runtime-action HTTP/1.1\r\n"
                "Host: 127.0.0.1:{0}\r\n"
                "Content-Type: application/x-www-form-urlencoded\r\n"
                "Content-Length: {1}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).format(port, declared_length).encode("ascii")
            sock.sendall(head)
            if body:
                sock.sendall(body)
            if not stall:
                # Signal EOF while keeping the read side open so the server's
                # read() returns short instead of resetting on write.
                sock.shutdown(socket.SHUT_WR)
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
            status_line = response.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            return int(status_line.split(" ", 2)[1]), response
        finally:
            sock.close()

    def test_truncated_body_returns_400(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                server, thread, port = self._start_server(root)
                try:
                    status, response = self._raw_post(port, declared_length=1024, body=b"csrf_token=abc")
                finally:
                    self._stop_server(server, thread)

        self.assertEqual(status, 400)
        content = response.decode("utf-8", errors="replace")
        self.assertIn("truncated", content.lower())
        self.assertNotIn("Traceback", content)
        self.assertNotIn('File "', content)

    def test_stalled_partial_body_returns_408(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                server, thread, port = self._start_server(root)
                try:
                    with patch("stock_agent.review_workbench.REQUEST_BODY_READ_TIMEOUT", 0.2):
                        status, response = self._raw_post(port, declared_length=1024, body=b"csrf_token=abc", stall=True)
                finally:
                    self._stop_server(server, thread)

        self.assertEqual(status, 408)
        content = response.decode("utf-8", errors="replace")
        self.assertIn("Timeout", content)
        self.assertNotIn("Traceback", content)
        self.assertNotIn('File "', content)


if __name__ == "__main__":
    unittest.main()
