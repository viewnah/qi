from __future__ import annotations

import json
from pathlib import Path

import pytest

from qi_kb import config
from qi_kb.errors import Conflict, NotFound


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def kb(kb_id: str, name: str) -> dict:
    return {"id": kb_id, "name": name, "docCount": 1, "chunkCount": 2}


def test_merge_global_and_project(kb_home, project):
    write(kb_home / "kb.json", {
        "providers": {
            "alice": {
                "baseUrl": "http://10.1.43.6:7091",
                "apiPrefix": "/api",
                "login": "alice",
                "knowledgeBases": {"kb1": kb("kb1", "全局 KB")},
            }
        },
        "defaults": {"topK": 5, "useRerank": True},
    })
    write(project / ".qi" / "kb.json", {
        "providers": {
            "alice": {
                "login": "alice-project",
                "knowledgeBases": {
                    "kb1": kb("kb1", "项目覆盖"),
                    "kb2": kb("kb2", "项目新增"),
                },
            }
        },
        "defaults": {"topK": 10},
    })
    cfg = config.load(project)
    assert cfg.providers["alice"]["login"] == "alice-project"
    assert cfg.providers["alice"]["baseUrl"] == "http://10.1.43.6:7091"
    assert set(cfg.providers["alice"]["knowledgeBases"]) == {"kb1", "kb2"}
    assert cfg.providers["alice"]["knowledgeBases"]["kb1"]["name"] == "项目覆盖"
    assert cfg.defaults["topK"] == 10
    assert cfg.defaults["useRerank"] is True


def test_duplicate_same_instance_is_collapsed(kb_home):
    write(kb_home / "kb.json", {
        "providers": {
            "alice": {"baseUrl": "http://h:7091", "knowledgeBases": {"kb1": kb("kb1", "A")}},
            "bob": {"baseUrl": "http://h:7091", "knowledgeBases": {"kb1": kb("kb1", "B")}},
        }
    })
    cfg = config.load()
    refs = config.iter_kbs(cfg)
    assert len(refs) == 1
    assert refs[0].provider == "alice"
    assert any("同一 KB" in warning for warning in cfg.warnings)


def test_same_id_different_instance_is_distinct(kb_home):
    write(kb_home / "kb.json", {
        "providers": {
            "alice": {"baseUrl": "http://a:7091", "knowledgeBases": {"kb1": kb("kb1", "A")}},
            "bob": {"baseUrl": "http://b:7091", "knowledgeBases": {"kb1": kb("kb1", "B")}},
        }
    })
    cfg = config.load()
    assert len(config.iter_kbs(cfg)) == 2


def test_auth_in_kb_json_is_ignored(kb_home):
    write(kb_home / "kb.json", {
        "providers": {
            "alice": {
                "baseUrl": "http://h:7091",
                "auth": {"access": "secret"},
                "knowledgeBases": {},
            }
        }
    })
    cfg = config.load()
    assert "auth" not in cfg.providers["alice"]
    assert any("kb-auth.json" in warning for warning in cfg.warnings)


def test_selector_by_id_name_and_provider(kb_home):
    write(kb_home / "kb.json", {
        "providers": {
            "alice": {"baseUrl": "http://h:7091", "knowledgeBases": {"kb1": kb("kb1", "项目手册")}},
            "bob": {"baseUrl": "http://h:7091", "knowledgeBases": {"kb2": kb("kb2", "项目手册")}},
        }
    })
    cfg = config.load()
    # 同名跨 provider -> 歧义
    with pytest.raises(NotFound):
        config.find_kb(cfg, "项目手册")
    assert config.find_kb(cfg, "项目手册@alice").kb_id == "kb1"
    assert config.find_kb(cfg, "kb2").provider == "bob"


def test_install_conflict_and_rebind(kb_home):
    from qi_kb import providers

    write(kb_home / "kb.json", {
        "providers": {
            "alice": {"baseUrl": "http://h:7091", "knowledgeBases": {"kb1": kb("kb1", "A")}},
            "bob": {"baseUrl": "http://h:7091", "knowledgeBases": {}},
        }
    })
    with pytest.raises(Conflict) as exc:
        providers.install_kb(None, "bob", kb("kb1", "A"))
    assert exc.value.existing_provider == "alice"

    result = providers.install_kb(None, "bob", kb("kb1", "A"), rebind=True)
    assert result["action"] == "rebind"
    cfg = config.load()
    assert "kb1" not in cfg.providers["alice"]["knowledgeBases"]
    assert "kb1" in cfg.providers["bob"]["knowledgeBases"]


def test_uninstall_keeps_provider(kb_home):
    from qi_kb import providers

    write(kb_home / "kb.json", {
        "providers": {
            "alice": {"baseUrl": "http://h:7091", "knowledgeBases": {"kb1": kb("kb1", "A")}},
        }
    })
    providers.uninstall_kb(None, "kb1")
    cfg = config.load()
    assert cfg.providers["alice"]["knowledgeBases"] == {}


def test_remove_provider_requires_cascade(kb_home):
    from qi_kb import providers

    write(kb_home / "kb.json", {
        "providers": {
            "alice": {"baseUrl": "http://h:7091", "knowledgeBases": {"kb1": kb("kb1", "A")}},
        }
    })
    with pytest.raises(Conflict):
        providers.remove_provider(None, "alice")
    result = providers.remove_provider(None, "alice", cascade=True)
    assert result["knowledgeBases"] == ["kb1"]
    assert config.load().providers == {}
