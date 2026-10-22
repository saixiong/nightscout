from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from night_scout.core.config import Config, load_config, save_config, split_ref
from night_scout.core.llm import OllamaClient
from night_scout.core.providers import FallbackLLM, OpenAICompatClient, make_embedder, make_llm

# The shape of the live demo config today: only `model:`, no providers/roles.
DEMO_STYLE = """\
model:
  host: http://127.0.0.1:11434
  name: gemma4:12b
  fallback: gemma4:e4b
  embed: embeddinggemma-2:270m
  timeout_s: 300.0
  keep_alive: 5m
thresholds:
  max_scored: 40
  relevance: 0.6
  digest_size: 7
  max_age_days: 3
  near_duplicate: 0.92
projects:
- path: /tmp/projects/llm
sources:
  rss:
  - name: Simon Willison
    url: https://simonwillison.net/atom/everything/
  hackernews:
    lists:
    - top
"""


def _demo(tmp_path: Path) -> Config:
    path = tmp_path / "config.yaml"
    path.write_text(DEMO_STYLE, encoding="utf-8")
    return load_config(path)


def test_demo_style_config_derives_ollama_provider_and_roles(tmp_path: Path) -> None:
    cfg = _demo(tmp_path)
    assert cfg.providers == []
    [p] = cfg.all_providers()
    assert (p.id, p.kind, p.base_url) == ("ollama", "ollama", "http://127.0.0.1:11434")
    assert cfg.role_ref("judge") == "ollama/gemma4:12b"
    assert cfg.role_ref("fallback") == "ollama/gemma4:e4b"
    assert cfg.role_ref("chat") == "ollama/gemma4:12b"
    assert cfg.role_ref("embed") == "ollama/embeddinggemma-2:270m"


def test_demo_style_config_builds_the_same_ollama_client(tmp_path: Path) -> None:
    cfg = _demo(tmp_path)
    llm = make_llm(cfg)
    assert isinstance(llm, OllamaClient)
    assert llm.cfg == cfg.model  # identical settings: host, name, fallback, embed, timeouts
    emb = make_embedder(cfg)
    assert isinstance(emb, OllamaClient) and emb.cfg == cfg.model
    llm.close()
    emb.close()


def test_saving_a_demo_style_config_adds_no_sections(tmp_path: Path) -> None:
    cfg = _demo(tmp_path)
    out = tmp_path / "saved.yaml"
    save_config(cfg, out)
    data = yaml.safe_load(out.read_text())
    assert "providers" not in data and "roles" not in data
    assert data["model"]["name"] == "gemma4:12b"


def test_lan_ollama_host_in_old_config_still_loads() -> None:
    cfg = Config.model_validate({"model": {"host": "http://192.168.1.20:11434"}})
    assert cfg.provider("ollama").base_url == "http://192.168.1.20:11434"


def test_providers_and_roles_round_trip(tmp_path: Path) -> None:
    raw = {
        "providers": [
            {
                "id": "lmstudio",
                "kind": "openai_compatible",
                "name": "LM Studio",
                "base_url": "http://127.0.0.1:1234/v1/",
            },
            {
                "id": "openrouter",
                "name": "OpenRouter",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            },
        ],
        "roles": {"judge": "openrouter/google/gemma-3-27b-it", "embed": "lmstudio/nomic-embed"},
    }
    cfg = Config.model_validate(raw)
    assert cfg.provider("lmstudio").base_url == "http://127.0.0.1:1234/v1"
    assert [p.id for p in cfg.all_providers()] == ["ollama", "lmstudio", "openrouter"]
    assert split_ref(cfg.role_ref("judge") or "") == ("openrouter", "google/gemma-3-27b-it")
    assert cfg.role_ref("chat") == "openrouter/google/gemma-3-27b-it"
    assert cfg.role_ref("fallback") == "ollama/gemma4:e4b"  # unset roles come from model:
    path = tmp_path / "c.yaml"
    save_config(cfg, path)
    text = path.read_text()
    assert "api_key:" not in text
    assert load_config(path) == cfg


def test_split_ref_keeps_ollama_tags() -> None:
    assert split_ref("ollama/gemma4:12b") == ("ollama", "gemma4:12b")
    for bad in ("gemma4:12b", "/x", "ollama/", "Bad Id/x", "ollama/ x"):
        with pytest.raises(ValueError):
            split_ref(bad)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1",  # remote must be https
        "ftp://127.0.0.1/v1",
        "file:///etc/passwd",
        "https://user:pw@example.com/v1",
        "https://example.com/v1?key=abc",
        "not a url",
    ],
)
def test_bad_base_urls_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"providers": [{"id": "x", "base_url": url}]})


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:1234/v1",
        "http://127.0.0.1:1234/v1",
        "http://[::1]:8080/v1",
        "https://openrouter.ai/api/v1",
    ],
)
def test_good_base_urls(url: str) -> None:
    Config.model_validate({"providers": [{"id": "x", "base_url": url}]})


def test_unknown_provider_and_duplicates_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown provider"):
        Config.model_validate({"roles": {"judge": "nope/model"}})
    dup = {"id": "a", "base_url": "http://127.0.0.1:1/v1"}
    with pytest.raises(ValidationError, match="duplicate"):
        Config.model_validate({"providers": [dup, dup]})
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "providers": [
                    {"id": "a", "base_url": "http://127.0.0.1:1", "api_key_env": "NOT-AN-ENV"}
                ]
            }
        )


def test_make_llm_role_routing(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-abcdefghijklmnop")
    cfg = Config.model_validate(
        {
            "providers": [
                {"id": "lmstudio", "base_url": "http://127.0.0.1:1234/v1"},
                {
                    "id": "openrouter",
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key_env": "OPENROUTER_API_KEY",
                },
            ],
            "roles": {
                "judge": "openrouter/google/gemma-3-27b-it",
                "fallback": "ollama/gemma4:e4b",
                "chat": "lmstudio/qwen3-8b",
                "embed": "lmstudio/nomic-embed-text",
            },
        }
    )
    judge = make_llm(cfg, "judge")
    assert isinstance(judge, FallbackLLM)
    assert isinstance(judge.primary, OpenAICompatClient)
    assert judge.primary.model_name == "google/gemma-3-27b-it"
    assert isinstance(judge.fallback, OllamaClient)
    assert judge.fallback.cfg.name == "gemma4:e4b" and judge.fallback.cfg.fallback == ""
    assert "sk-or" not in repr(judge)

    chat = make_llm(cfg, "chat")
    assert isinstance(chat, FallbackLLM) and chat.primary.model_name == "qwen3-8b"
    fb = make_llm(cfg, "fallback")
    assert isinstance(fb, OllamaClient) and fb.model_name == "gemma4:e4b"

    emb = make_embedder(cfg)
    assert isinstance(emb, OpenAICompatClient) and emb.embed_model == "nomic-embed-text"
    with pytest.raises(ValueError):
        make_llm(cfg, "embed")


def test_make_llm_same_ollama_fallback_stays_inside_ollama_client() -> None:
    cfg = Config.model_validate(
        {"roles": {"judge": "ollama/qwen3:8b", "fallback": "ollama/gemma4:e4b"}}
    )
    llm = make_llm(cfg)
    assert isinstance(llm, OllamaClient)
    assert (llm.cfg.name, llm.cfg.fallback, llm.cfg.embed) == (
        "qwen3:8b",
        "gemma4:e4b",
        "embeddinggemma-2:270m",
    )
