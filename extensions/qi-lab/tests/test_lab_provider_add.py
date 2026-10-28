"""`POST /api/providers`:新建一个**用户级** provider(写 models.json + auth.json)。

三条边界,每条一条用例:

1. **要过确认闸**:没 `X-Qi-Confirm: yes` → 428,文件一个字节都不动(与写凭证同一条);
2. **写下去就得能用**:名字 / baseUrl / api / 模型 id / key 任一不合格 → 400,
   不落半份配置(provider 声明与凭证要么都成,要么都不留);
3. **只有用户级**:同名 provider 已在别的 models.json 里定义 → 409,不往用户级再写一份
   (写用户级也盖不住高优先级文件,“加了不生效”比拒绝更贵)。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from qi_agent import paths
from qi_agent.auth import AuthStore
from qi_agent.config import load_models_file

pytest.importorskip("fastapi")   # qi-lab 的 API 层依赖;核心 venv(只装 qi)不带它
from qi_lab.app import create_app
from qi_lab.state import WebState

YES = {"X-Qi-Confirm": "yes"}


def _client(tmp_path: Path) -> httpx.AsyncClient:
    app = create_app(cwd=tmp_path, state=WebState(tmp_path), password=None,
                     allowed_hosts=[], bind_host="127.0.0.1")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://127.0.0.1")


def _global_models() -> Path:
    return Path(paths.global_home()) / paths.MODELS_FILE_NAME


def _body(**over) -> dict:
    body = {
        "name": "acme",
        "base_url": "https://api.acme.test/v1",
        "api": "openai-completions",
        "api_key": "sk-acme-123456",
        "models": ["acme-a", "acme-b"],
    }
    body.update(over)
    return body


@pytest.mark.asyncio
async def test_add_provider_requires_confirm(minimal_config, tmp_path):
    async with _client(tmp_path) as client:
        response = await client.post("/api/providers", json=_body())

    assert response.status_code == 428
    assert "acme" not in load_models_file(_global_models()).get("providers", {})
    assert AuthStore().get("acme") is None        # 凭证不写


@pytest.mark.asyncio
async def test_add_provider_writes_models_and_credential(minimal_config, tmp_path):
    async with _client(tmp_path) as client:
        response = await client.post("/api/providers", headers=YES, json=_body())

    assert response.status_code == 200
    entry = next(p for p in response.json()["providers"] if p["name"] == "acme")
    assert entry["credential_ok"] is True
    assert entry["model_ids"] == ["acme-a", "acme-b"]
    assert "sk-acme-123456" not in response.text

    written = load_models_file(_global_models())["providers"]["acme"]
    assert written["baseUrl"] == "https://api.acme.test/v1"
    assert written["api"] == "openai-completions"
    assert [item["id"] for item in written["models"]] == ["acme-a", "acme-b"]
    assert "apiKey" not in written                 # 凭证只进 auth.json
    assert AuthStore().get("acme") == "sk-acme-123456"


@pytest.mark.asyncio
@pytest.mark.parametrize("over", [
    {"name": ""},
    {"name": "bad name"},
    {"base_url": "ftp://x"},
    {"api": "not-an-api"},
    {"models": []},
    {"api_key": ""},
])
async def test_add_provider_rejects_bad_body(minimal_config, tmp_path, over):
    async with _client(tmp_path) as client:
        response = await client.post("/api/providers", headers=YES, json=_body(**over))

    assert response.status_code == 400
    assert AuthStore().get("acme") is None


@pytest.mark.asyncio
async def test_add_provider_refuses_name_defined_elsewhere(minimal_config, tmp_path):
    """项目级 models.json 已经写过同名 provider → 409,不往用户级再写一份。"""
    project = tmp_path / ".qi"
    project.mkdir(parents=True)
    (project / paths.MODELS_FILE_NAME).write_text(json.dumps({"providers": {
        "acme": {"api": "openai-completions", "baseUrl": "https://old.example/v1",
                 "models": [{"id": "old"}]}}}), encoding="utf-8")

    async with _client(tmp_path) as client:
        response = await client.post("/api/providers", headers=YES, json=_body())

    assert response.status_code == 409
    assert "old.example" in response.text or "定义" in response.text
    assert AuthStore().get("acme") is None
