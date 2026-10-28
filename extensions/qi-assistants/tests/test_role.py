"""角色样例：能装上、能被 qi-agents 发现、工具名单只指向本扩展的两个工具。"""

from __future__ import annotations

import shutil
from pathlib import Path

from qi_agents.discovery import discover

REPO = Path(__file__).resolve().parents[3]
ROLE = REPO / "extensions" / "qi-agents" / "qi_agents" / "builtin" / "agents" / "training-evaluator"


def test_register_adds_both_tools():
    from qi_assistants import register

    class Api:
        def __init__(self) -> None:
            self.tools = []

        def register_tool(self, tool) -> None:
            self.tools.append(tool)

    api = Api()
    register(api)
    assert [tool.name for tool in api.tools] == ["assistant_search", "assistant_recommend"]


def test_role_is_discoverable_with_expected_tools(tmp_path):
    target = tmp_path / ".qi" / "agents" / "training-evaluator"
    shutil.copytree(ROLE, target)
    roles = discover(tmp_path, "project", include_remote=False)
    role = roles["训练评估"]
    assert role.tools == ["assistant_search", "assistant_recommend"]
    assert role.opening is not None
    assert "评估" in role.opening.message
    assert role.opening.suggestions
