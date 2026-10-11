"""Synthetic identity and genuine temporary vault/task producers for review tests."""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from stock_agent.config import project_path_override
from stock_agent.evidence_excerpts import create_excerpt, excerpt_selector
from stock_agent.summary_review import review_draft, submit_draft
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec
from _vault_fixture import vault_config, import_document

AUTHOR = "human:synthetic-author"
REVIEWER = "human:synthetic-reviewer"
SID_A = "S-1-5-21-101-102-103-1001"
SID_R = "S-1-5-21-101-102-103-1002"


class SummaryFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root)
        self.ref, self.sha = import_document(self.config, self.root, content="Revenue decreased. Synthetic local source.", title="Synthetic source")
        self.selector = excerpt_selector(create_excerpt(self.config, self.ref, 0, 18))
        self.spec = build_task_spec(research_question="Synthetic review", symbols=["600519"], config=self.config)
        self.roster = {"principals": [self.principal(AUTHOR, SID_A), self.principal(REVIEWER, SID_R)]}
        self.save_roster()
        operations = self.config["research_operations"]
        operations.update(enabled=True, enforce_roster=True)
        Path(operations["restrictions_path"]).write_text(json.dumps({"entries": []}), encoding="utf-8")
        self.db = self.root / "tasks.sqlite3"
        self.config["task_runtime"] = {"db_path": str(self.db)}
        self.store = TaskStore(self.db)
        self.addCleanup(self.store.close)
        created, _ = self.store.create_task(title="Synthetic task", input_payload=self.spec, execution_mode="replay")
        self.task_id = created["task_id"]
        leased = self.store.claim_next("worker-fixture")
        self.store.mark_running(self.task_id, worker_id="worker-fixture", fencing_token=leased["fencing_token"], expected_version=leased["version"])
        self.store.request_awaiting_human(self.task_id, worker_id="worker-fixture", fencing_token=leased["fencing_token"],
                                         human_request={"question": "Review synthetic draft?"},
                                         output={"summary": "Synthetic research draft"}, model_call={"mode": "replay", "called": False})

    def principal(self, actor, sid):
        return {"actor_id": actor, "principal_type": "human", "active": True,
                "roles": ["information_analyst"], "information_side": "public", "coverage_entities": ["600519"],
                "authorizations": ["summary_review_authorized"], "max_active_cases": 10,
                "identity": {"provider": "windows_local", "subject": sid, "bound_at": "2026-09-20T09:00:00+08:00",
                             "bound_evidence_ref": "control:synthetic-only"}}

    def save_roster(self):
        Path(self.config["research_operations"]["roster_path"]).write_text(json.dumps(self.roster), encoding="utf-8")

    def version(self):
        return self.store.get_task(self.task_id)["version"]

    def submit(self, **overrides):
        args = dict(config=self.config, actor=AUTHOR, selectors=[self.selector], text="Synthetic research draft",
                    kind="model_inference", expected_version=self.version(), command_id="submit-1")
        args.update(overrides)
        with patch("stock_agent.local_identity.current_windows_subject", return_value=SID_A):
            return submit_draft(self.store, self.task_id, **args)

    def decide(self, draft, **overrides):
        args = dict(config=self.config, actor=REVIEWER, draft_id=draft["draft_id"], summary_id=draft["summary_id"],
                    decision="supported", notes="Synthetic reviewer judgment, not a business approval",
                    expected_version=self.version(), command_id="review-1")
        args.update(overrides)
        with patch("stock_agent.local_identity.current_windows_subject", return_value=SID_R):
            return review_draft(self.store, self.task_id, **args)
