"""Cross-service coverage for project-defined model shortcuts."""

import importlib
import json
import os
import sys

import pytest


SERVICES_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "templates", "services")
)
if SERVICES_DIR not in sys.path:
    sys.path.insert(0, SERVICES_DIR)


def _service(module_name, class_name):
    module = importlib.import_module(module_name)
    return getattr(module, class_name)()


@pytest.mark.parametrize(
    ("subagent", "module_name", "class_name", "shipped", "shipped_target"),
    [
        ("claude", "claude", "ClaudeService", ":sonnet", "claude-sonnet-4-6"),
        ("cursor", "claude", "ClaudeService", ":sonnet", "claude-sonnet-4-6"),
        ("codex", "codex", "CodexService", ":codex", "gpt-5.3-codex"),
        ("gemini", "gemini", "GeminiService", ":pro", "gemini-2.5-pro"),
        ("pi", "pi", "PiService", ":gpt", "openai-codex/gpt-6-astra"),
    ],
)
def test_project_shortcuts_are_subagent_specific_and_keep_shipped_fallbacks(
    monkeypatch, subagent, module_name, class_name, shipped, shipped_target
):
    configured = {
        name: {":fav": f"provider/{name}-favorite"}
        for name in ("claude", "cursor", "codex", "gemini", "pi")
    }
    monkeypatch.setenv("JUNO_MODEL_SHORTCUTS", json.dumps(configured))
    monkeypatch.setenv("JUNO_SELECTED_SUBAGENT", subagent)
    service = _service(module_name, class_name)

    assert service.expand_model_shorthand(":fav") == f"provider/{subagent}-favorite"
    assert service.expand_model_shorthand(shipped) == shipped_target


def test_project_shortcut_overrides_shipped_and_supports_chaining(monkeypatch):
    monkeypatch.setenv(
        "JUNO_MODEL_SHORTCUTS",
        json.dumps({"pi": {":gpt": ":fav", ":fav": "zai/glm-5.3"}}),
    )
    service = _service("pi", "PiService")

    assert service.expand_model_shorthand(":gpt") == "zai/glm-5.3"


def test_project_shortcut_can_chain_to_a_shipped_shortcut(monkeypatch):
    monkeypatch.setenv("JUNO_MODEL_SHORTCUTS", json.dumps({"claude": {":fav": ":opus"}}))
    monkeypatch.setenv("JUNO_SELECTED_SUBAGENT", "claude")

    assert _service("claude", "ClaudeService").expand_model_shorthand(":fav") == "claude-opus-4-6"


def test_project_shortcut_cycle_is_rejected(monkeypatch):
    monkeypatch.setenv(
        "JUNO_MODEL_SHORTCUTS",
        json.dumps({"codex": {":first": ":second", ":second": ":first"}}),
    )
    from environment_boundary import ModelShortcutError

    with pytest.raises(ModelShortcutError, match=r":first -> :second -> :first"):
        _service("codex", "CodexService").expand_model_shorthand(":first")


def test_no_project_configuration_keeps_shipped_and_full_model_behavior(monkeypatch):
    monkeypatch.delenv("JUNO_MODEL_SHORTCUTS", raising=False)

    service = _service("gemini", "GeminiService")
    assert service.expand_model_shorthand(":flash") == "gemini-2.5-flash"
    assert service.expand_model_shorthand("provider/full-model") == "provider/full-model"


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        "[]",
        json.dumps({"unknown": {":fav": "provider/model"}}),
        json.dumps({"pi": []}),
        json.dumps({"pi": {"fav": "provider/model"}}),
        json.dumps({"pi": {":fav": ""}}),
    ],
)
def test_malformed_external_environment_is_rejected(monkeypatch, value):
    monkeypatch.setenv("JUNO_MODEL_SHORTCUTS", value)

    from environment_boundary import ModelShortcutError

    with pytest.raises(ModelShortcutError, match="malformed JUNO_MODEL_SHORTCUTS"):
        _service("pi", "PiService").expand_model_shorthand(":gpt")


def test_unknown_shortcut_is_rejected(monkeypatch):
    monkeypatch.delenv("JUNO_MODEL_SHORTCUTS", raising=False)

    from environment_boundary import ModelShortcutError

    with pytest.raises(ModelShortcutError, match=r"unknown model shortcut for codex: :missing"):
        _service("codex", "CodexService").expand_model_shorthand(":missing")
