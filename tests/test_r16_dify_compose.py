"""Offline checks of the R16 deployment files and the frozen workflow boundary."""

import json
import re
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import yaml

from stock_agent.dify_api import DifyGateway


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy/dify"
FLOW = ROOT / "config/candidates/dify/flows/single-research-replay.yml"


class R16ComposeTests(unittest.TestCase):
    def setUp(self):
        self.compose = yaml.safe_load((DEPLOY / "compose.yaml").read_text(encoding="utf-8"))
        self.services = self.compose["services"]

    def test_release_services_use_pinned_images(self):
        upstream = {
            "init_permissions", "api", "api_websocket", "worker", "worker_beat",
            "web", "db_postgres", "redis", "weaviate", "sandbox", "plugin_daemon",
            "agent_backend", "local_sandbox", "agent_ssrf_proxy", "ssrf_proxy", "nginx",
        }
        self.assertEqual(
            set(self.services),
            upstream | {"replay_state_init", "research_api", "stock-agent-r16-gateway"},
        )
        manifest = json.loads(FLOW.with_suffix(".manifest.json").read_text(encoding="utf-8"))
        release = manifest["dify_release"]
        for service, image in {
            "api": "dify-api", "api_websocket": "dify-api", "worker": "dify-api",
            "worker_beat": "dify-api", "web": "dify-web",
            "agent_backend": "dify-agent-backend", "local_sandbox": "dify-agent-local-sandbox",
        }.items():
            self.assertTrue(self.services[service]["image"].startswith(f"langgenius/{image}:{release}@"))
        for service in self.services.values():
            self.assertRegex(service["image"], r":[^@\s]+@sha256:[a-f0-9]{64}$")
            self.assertNotIn(":latest@", service["image"])
            self.assertNotIn("env_file", service)
            self.assertNotIn("build", service)

    def test_network_membership_and_python_state(self):
        networks = self.compose["networks"]
        members = {
            name: {key for key, service in self.services.items() if name in service.get("networks", [])}
            for name in networks
        }
        self.assertEqual(members, {
            "ui_edge": {"nginx"},
            "core": {"agent_backend", "agent_ssrf_proxy", "api", "api_websocket", "db_postgres",
                     "nginx", "plugin_daemon", "redis", "weaviate", "web", "worker", "worker_beat"},
            "code_sandbox": {"api", "api_websocket", "sandbox", "worker"},
            "workflow_proxy": {"api", "api_websocket", "ssrf_proxy", "worker"},
            "replay_link": {"ssrf_proxy", "stock-agent-r16-gateway"},
            "agent_control": {"agent_backend", "local_sandbox"},
            "agent_proxy": {"agent_ssrf_proxy", "local_sandbox"},
        })
        for name in members.keys() - {"ui_edge"}:
            self.assertIs(networks[name]["internal"], True)
            self.assertEqual(
                networks[name]["driver_opts"]["com.docker.network.bridge.gateway_mode_ipv4"],
                "isolated",
            )
        self.assertEqual(
            {key: service["ports"] for key, service in self.services.items() if "ports" in service},
            {"nginx": ["127.0.0.1:18080:80"]},
        )
        self.assertEqual(
            {key: service["network_mode"] for key, service in self.services.items() if "network_mode" in service},
            {"research_api": "service:stock-agent-r16-gateway",
             "init_permissions": "none", "replay_state_init": "none"},
        )
        research = self.services["research_api"]
        self.assertEqual(set(research["environment"]), {"R16_APPROVED_WORKFLOW_VERSION"})
        self.assertIs(research["read_only"], True)
        self.assertEqual(
            {key for key, service in self.services.items()
             if any(isinstance(mount, str) and mount.startswith("replay_state:")
                    for mount in service.get("volumes", []))},
            {"replay_state_init", "research_api"},
        )
        self.assertEqual(
            json.loads((DEPLOY / "replay-credentials.example.json").read_text(encoding="utf-8")),
            {"schema_version": "1.0", "credentials": []},
        )

    def test_ssrf_and_exact_workflow_routes(self):
        for name in ("api", "api_websocket", "worker"):
            env = self.services[name]["environment"]
            for scheme in ("HTTP", "HTTPS"):
                self.assertEqual(env[f"SSRF_PROXY_{scheme}_URL"], "http://ssrf_proxy:3128")
            self.assertEqual(env["SSRF_DEFAULT_MAX_RETRIES"], "1")
        self.assertEqual(self.services["sandbox"]["environment"]["ENABLE_NETWORK"], "false")
        plugin = self.services["plugin_daemon"]["environment"]
        self.assertEqual(plugin["PLUGIN_REMOTE_INSTALLING_HOST"], "127.0.0.1")
        self.assertEqual(plugin["PLUGIN_REMOTE_INSTALLING_PORT"], "5003")

        proxy = (DEPLOY / "squid-replay.conf").read_text(encoding="utf-8")
        gateway = (DEPLOY / "nginx-replay.conf").read_text(encoding="utf-8")
        expression = re.search(r"^acl replay_url url_regex (.+)$", proxy, re.MULTILINE)[1]
        workflow = yaml.safe_load(FLOW.read_text(encoding="utf-8"))
        requests = [node["data"] for node in workflow["workflow"]["graph"]["nodes"]
                    if node["data"]["type"] == "http-request"]
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertEqual(request["method"].lower(), "post")
            self.assertRegex(request["url"], expression)
            path = urlsplit(request["url"]).path
            self.assertIn(f"~^POST:{path}$ 1;", gateway)
            self.assertNotRegex(request["url"] + "?other=1", expression)
        self.assertNotRegex("http://stock-agent-r16-gateway:8770/api/dify/v1/fact-bundle", expression)
        self.assertEqual(
            re.findall(r"^http_access .+$", proxy, re.MULTILINE),
            ["http_access allow post replay_port replay_host replay_url", "http_access deny all"],
        )
        self.assertIn("forward_max_tries 1", proxy)
        self.assertIn("proxy_next_upstream off;", gateway)
        self.assertIn("proxy_pass http://127.0.0.1:8771;", gateway)


class ReplayGatewayTests(unittest.TestCase):
    def test_only_replay_and_status_only_are_handled(self):
        gateway_type = runpy.run_path(str(DEPLOY / "replay_api.py"))["ReplayGateway"]
        gateway = gateway_type.__new__(gateway_type)
        with patch.object(DifyGateway, "create_research") as create:
            with self.assertRaisesRegex(ValueError, "replay tasks only"):
                gateway.create_research("fixture", execution_mode="live")
            with self.assertRaisesRegex(ValueError, "replay tasks only"):
                gateway.create_research("fixture")
            create.assert_not_called()
            gateway.create_research("fixture", execution_mode="replay", title="Fixture")
            create.assert_called_once_with("fixture", execution_mode="replay", title="Fixture")
        with patch.object(DifyGateway, "research_status") as status:
            with self.assertRaisesRegex(ValueError, "status_only"):
                gateway.research_status("fixture", task_id="task", status_only=False)
            status.assert_not_called()
            gateway.research_status("fixture", task_id="task", status_only=True)
            status.assert_called_once_with("fixture", task_id="task", status_only=True)
