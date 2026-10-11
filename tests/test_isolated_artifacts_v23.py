import tempfile
import unittest
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.research_git import stage_llm_research_drafts


class IsolatedArtifactsV23Tests(unittest.TestCase):
    def test_disabled_research_git_does_not_read_or_write_shared_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with project_path_override(lambda value: root / value):
                stats = stage_llm_research_drafts([], {"research_git": {"enabled": False}})

            self.assertFalse((root / "data/research_git").exists())
            self.assertFalse((root / "reports/research_git").exists())
            self.assertEqual(0, stats["research_git_draft_count"])
            self.assertEqual("", stats["research_git_summary_latest_path"])


if __name__ == "__main__":
    unittest.main()
