import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import version  # noqa: E402


def test_site_version_matches_agent_version():
    agent = (ROOT.parent / "agent" / "host_agent.py").read_text(encoding="utf-8")
    assert re.search(r'^AGENT_VERSION = "([^"]+)"', agent, re.M).group(1) == version.VERSION
    assert version.VERSION in version.CHANGES
