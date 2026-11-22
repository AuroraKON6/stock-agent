"""Offline checks of the frozen Dify replay artifact and its Python contract."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from stock_agent.dify_api import DifyGateway
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
            "external request": lambda flow: flow["workflow"]["graph"]["nodes"][2]["data"].update(
                url="https://example.com/"
            ),
            "automatic retry": lambda flow: flow["workflow"]["graph"]["nodes"][2]["data"]["retry_config"].update(
                retry_enabled=True
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
        import yaml

        manifest = load_manifest()
        self.workflow_version = validate_replay_workflow(MANIFEST)
        dsl = yaml.safe_load((FLOW_DIR / manifest["artifact"]).read_bytes())
        self.code = {}
        for node in dsl["workflow"]["graph"]["nodes"]:
            data = node["data"]
            if data["type"] == "code":
                namespace = {}
                exec(data["code"], namespace)
                self.code[data["title"]] = namespace["main"]
        self.gateway = DifyGateway(
            self.store, self._config_with(approved_workflow_versions=[self.workflow_version])
        )

    def _create_replay(self, run_id):
        request = self.code["Build replay request"](
            "公开事件", "case-alpha", '公告的影响？"含引号"\n下一行', 1,
            "case-alpha-first-research", run_id, self.workflow_version
        )
        return self.gateway.create_research(gateway_test.TOKEN, **json.loads(request["body"]))

    def test_two_platform_runs_share_one_case_task(self):
        first = self._create_replay("run-first")
        second = self._create_replay("run-second")
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(second["platform_run"]["task_run_count"], 2)
        task_id = first["task_id"]
        task = self.store.get_task(task_id)
        self.assertEqual(task["execution_mode"], "replay")
        self.assertEqual(task["input_payload"]["research_question"], '公告的影响？"含引号"\n下一行')
        decode = self.code["Check creation receipt"]
        response = json.dumps({"ok": True, "data": second})
        self.assertEqual(decode(response, 200, "case-alpha", "run-first")["can_query"], 0)
        output = decode(response, 200, "case-alpha", "run-second")
        self.assertEqual(output["can_query"], 1)
        self.assertEqual(output["case_path"], "/?task=" + task_id)
        status = self.gateway.research_status(gateway_test.TOKEN, **json.loads(output["status_body"]))
        self.assertEqual(status, {"task_id": task_id, "case_id": "case-alpha", "status": "queued"})
        self.assertEqual(
            self.code["Read business status"](json.dumps({"ok": True, "data": status}), 200, task_id, "case-alpha"),
            {"status": "queued"},
        )

    def test_transport_success_is_not_a_business_success(self):
        decode = self.code["Check creation receipt"]
        self.assertEqual(decode("{}", 0, "case-alpha", "run-first")["status"], "create_unconfirmed")
        self.assertEqual(decode("{}", 0, "case-alpha", "run-first")["can_query"], 0)
        status = self.code["Read business status"]
        args = ("task_0123456789abcdef", "case-alpha")
        self.assertEqual(status("{}", 0, *args), {"status": "status_unconfirmed"})
        self.assertEqual(status("{}", 200, *args), {"status": "status_unconfirmed"})
        failed = {"ok": True, "data": {"task_id": args[0], "case_id": args[1], "status": "failed"}}
        self.assertEqual(status(json.dumps(failed), 200, *args), {"status": "failed"})

