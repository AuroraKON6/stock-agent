"""Offline checks of the frozen Dify replay artifact and its Python contract."""

import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from stock_agent.dify_api import DifyGateway
from stock_agent.dify_api_server import create_dify_api_server
from stock_agent.dify_deployment import DeploymentCandidateError, validate_replay_workflow

import test_dify_api_v1 as gateway_test

FLOW_DIR = Path(__file__).resolve().parents[1] / "config/candidates/dify/flows"
MANIFEST = FLOW_DIR / "single-research-replay.manifest.json"


def load_manifest():
    return json.loads(MANIFEST.read_bytes())


class ReleaseArtifactTests(unittest.TestCase):
    def test_candidate_matches_reviewed_digest(self):
        version = load_manifest()["workflow_version"]
        self.assertEqual(validate_replay_workflow(MANIFEST, approved_version=version), version)
        with self.assertRaisesRegex(DeploymentCandidateError, "digest"):
            validate_replay_workflow(MANIFEST, approved_version="sha256:" + "0" * 64)

    def test_updated_digest_does_not_allow_secrets_plugins_or_extra_requests(self):
        import yaml

        manifest = load_manifest()
        original = (FLOW_DIR / manifest["artifact"]).read_bytes()
        mutations = {
            "inline bearer key": lambda flow: flow["workflow"]["environment_variables"][0].update(value="injected"),
            "secret in metadata": lambda flow: flow["app"].update(description="sk-" + "rPmY7Dgf4MKLfU3aTninYybH2"),
            "plugin": lambda flow: flow.update(dependencies=[{"type": "marketplace"}]),
            "external request": lambda flow: flow["workflow"]["graph"]["nodes"][1]["data"].update(
                url="https://example.com/"
            ),
            "automatic retry": lambda flow: flow["workflow"]["graph"]["nodes"][1]["data"]["retry_config"].update(
                retry_enabled=True
            ),
            "code node": lambda flow: flow["workflow"]["graph"]["nodes"][2]["data"].update(type="code"),
            "query without confirmed creation": lambda flow: flow["workflow"]["graph"]["edges"][0].update(
                target="16006"
            ),
            "HTTP status alone": lambda flow: flow["workflow"]["graph"]["nodes"][2]["data"]["cases"][0][
                "conditions"
            ].pop(),
            "secret output": lambda flow: flow["workflow"]["graph"]["nodes"][5]["data"]["outputs"][0].update(
                value_selector=["env", "STOCK_AGENT_SERVICE_SECRET"]
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                candidate = yaml.safe_load(original)
                mutate(candidate)
                raw = yaml.safe_dump(candidate).encode()
                target = Path(temp) / MANIFEST.name
                target.with_name(manifest["artifact"]).write_bytes(raw)
                updated = dict(manifest, workflow_version="sha256:" + hashlib.sha256(raw).hexdigest())
                target.write_text(json.dumps(updated), encoding="utf-8")
                with self.assertRaises(DeploymentCandidateError):
                    validate_replay_workflow(target)


class ReplayContractTests(gateway_test.GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.workflow_version = validate_replay_workflow(MANIFEST)
        self.gateway = DifyGateway(
            self.store, self._config_with(approved_workflow_versions=[self.workflow_version])
        )
        self.server = create_dify_api_server(self.gateway, port=0, replay_forms=True)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)

    def _post_form(self, operation, fields, token=gateway_test.TOKEN):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            conn.request("POST", "/api/dify/v1/" + operation, urlencode(fields), {
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Bearer {token}",
            })
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), json.load(response)
        finally:
            conn.close()

    def test_two_platform_runs_share_one_case_task(self):
        question = '公告的影响？"含引号"\n下一行 + & %'
        fields = {
            "title": "公开事件", "case_id": "case-alpha", "research_question": question,
            "input_version": "1", "business_intent_id": "case-alpha-first-research",
            "dify_run_id": "run-first", "workflow_version": self.workflow_version,
            "execution_mode": "replay",
        }
        code, _, first_reply = self._post_form("create-research", fields)
        self.assertEqual(code, 200)
        code, headers, second_reply = self._post_form("create-research", {**fields, "dify_run_id": "run-second"})
        self.assertEqual(code, 200)
        first, second = first_reply["data"], second_reply["data"]
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(second["platform_run"]["task_run_count"], 2)
        self.assertEqual(second["platform_run"]["run_id"], "run-second")
        task_id = first["task_id"]
        task = self.store.get_task(task_id)
        self.assertEqual(task["execution_mode"], "replay")
        self.assertEqual(task["input_payload"]["research_question"], question)
        self.assertEqual(headers["R16_Task_Id"], task_id)
        self.assertEqual(headers["R16_Case_Path"], "/?task=" + task_id)
        code, headers, status = self._post_form("research-status", {"task_id": task_id, "status_only": "true"})
        self.assertEqual(code, 200)
        self.assertEqual(
            status["data"], {"task_id": task_id, "case_id": "case-alpha", "status": "queued"},
        )
        self.assertEqual(headers["R16_Status"], "queued")

        leased = self.store.claim_next("fixture-worker")
        self.store.fail(task_id, worker_id="fixture-worker", fencing_token=leased["fencing_token"],
                        error="synthetic failure", retryable=False)
        code, headers, status = self._post_form("research-status", {"task_id": task_id, "status_only": "true"})
        self.assertEqual(code, 200)
        self.assertEqual(status["data"]["status"], "failed")
        self.assertEqual(headers["R16_Status"], "failed")

    def test_form_rejects_duplicate_metadata_and_omits_receipt_on_denial(self):
        fields = [("task_id", "task_0123456789abcdef"), ("status_only", "true")]
        code, headers, _ = self._post_form("research-status", fields + [("status_only", "false")])
        self.assertEqual(code, 400)
        self.assertNotIn("R16_Status", headers)
        code, headers, _ = self._post_form("research-status", fields, token="unauthorized")
        self.assertEqual(code, 401)
        self.assertNotIn("R16_Status", headers)
