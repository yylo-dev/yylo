#!/usr/bin/env python3
"""Shared child-environment boundary; never emits environment values."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import threading
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping

_SCOPED_CONTINUITY_KEY_PREFIXES = (
    "YYLO_LAST_SESSION_ID_SCOPE_",
    "YYLO_LAST_EXECUTION_SETTINGS_SCOPE_",
    "JUNO_CODE_LAST_SESSION_ID_SCOPE_",
    "JUNO_CODE_LAST_EXECUTION_SETTINGS_SCOPE_",
)
_LEGACY_CONTINUITY_KEYS = frozenset(
    {
        "YYLO_LAST_SESSION_ID",
        "YYLO_LAST_EXECUTION_SETTINGS",
        "JUNO_CODE_LAST_SESSION_ID",
        "JUNO_CODE_LAST_EXECUTION_SETTINGS",
    }
)
_MODEL_SHORTCUTS_ENV = "JUNO_MODEL_SHORTCUTS"
_MODEL_SHORTCUT_ENV_KEYS = frozenset({_MODEL_SHORTCUTS_ENV, "JUNO_SELECTED_SUBAGENT"})
_MODEL_SHORTCUT_KEY = re.compile(r"^:[A-Za-z0-9_-]+$")
_MODEL_SHORTCUT_SUBAGENTS = frozenset({"claude", "cursor", "codex", "gemini", "pi"})

# An observation, not an authority assertion. Consume before any harness child
# inherits it; independent public wrappers always establish their own origin.
_STARTUP_ORIGIN_KEY = "YYLO_STARTUP_WRAPPER_EPOCH"
_STARTUP_ORIGIN = os.environ.pop(_STARTUP_ORIGIN_KEY, "")
_STARTUP_TIMING_ENABLED = os.environ.get("YYLO_STARTUP_TIMING") == "1"
_HANDOFF_COUNT = 0
_STARTUP_PROGRESS_KEY = 'YYLO_STARTUP_PROGRESS'
_PROGRESS_STOP = threading.Event()


def _startup_progress() -> None:
    # This daemon owns no processes and has no cancellation authority. It only
    # describes YYLO service work and stops at exec, not session readiness.
    while not _PROGRESS_STOP.is_set():
        try:
            # One small atomic write: no buffered Python stream lock can be
            # held by this daemon during interpreter shutdown on a primary error.
            os.write(2, 'YYLO: Preparing requested harness executable…\n'.encode('utf-8'))
        except OSError:
            return
        if _PROGRESS_STOP.wait(4):
            return


if os.environ.pop(_STARTUP_PROGRESS_KEY, '') == '1':
    try:
        threading.Thread(target=_startup_progress, daemon=True).start()
    except RuntimeError:
        pass  # Optional progress cannot prevent launch.


def record_harness_handoff(harness: str) -> None:
    """Best-effort exec completion, NOT harness/session/provider readiness.

    Call only after Popen succeeds at the actual harness executable. Python's
    exec-error pipe makes a failed exec raise instead of producing this sample.
    Wall-clock observations require an external monotonic fixture cross-check;
    clock adjustments and caller-supplied origins are not performance evidence.
    No paths, prompts, session IDs, model names or environment values are logged.
    """
    global _HANDOFF_COUNT
    _PROGRESS_STOP.set()
    if not _STARTUP_TIMING_ENABLED or harness not in _MODEL_SHORTCUT_SUBAGENTS:
        return
    try:
        if not re.fullmatch(r"[0-9]{1,12}\.[0-9]{1,6}", _STARTUP_ORIGIN):
            return
        origin_ns = int(Decimal(_STARTUP_ORIGIN) * 1_000_000_000)
        elapsed_ns = time.time_ns() - origin_ns
        if elapsed_ns < 0:
            return  # Clock moved backwards; do not invent a zero-duration pass.
        _HANDOFF_COUNT += 1
        event = {"event": "yylo_harness_handoff", "schema_version": 1,
                 "harness": harness, "launch_index": _HANDOFF_COUNT,
                 "clock": "unix_wall",
                 "origin_precision_us": 10 ** (6 - len(_STARTUP_ORIGIN.split(".")[1])),
                 "elapsed_ms": elapsed_ns / 1_000_000}
        print(json.dumps(event, separators=(",", ":")), file=sys.stderr, flush=True)
    except (OSError, ValueError, InvalidOperation, OverflowError):
        pass  # Optional diagnostics must never replace the launch outcome.


class ModelShortcutError(ValueError):
    """Raised when model-shortcut input cannot be safely resolved."""


def _project_model_shortcuts(
    subagent: str,
    environment: Mapping[str, str],
) -> dict[str, str]:
    raw = environment.get(_MODEL_SHORTCUTS_ENV)
    if raw is None:
        return {}
    try:
        configured = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as error:
        raise ModelShortcutError(f"malformed {_MODEL_SHORTCUTS_ENV}: expected JSON object") from error
    if not isinstance(configured, dict):
        raise ModelShortcutError(f"malformed {_MODEL_SHORTCUTS_ENV}: expected JSON object")
    unknown_subagents = sorted(set(configured) - _MODEL_SHORTCUT_SUBAGENTS)
    if unknown_subagents:
        raise ModelShortcutError(
            f"malformed {_MODEL_SHORTCUTS_ENV}: unknown subagent {unknown_subagents[0]}"
        )
    selected = configured.get(subagent, {})
    if not isinstance(selected, dict):
        raise ModelShortcutError(
            f"malformed {_MODEL_SHORTCUTS_ENV}: {subagent} shortcuts must be an object"
        )
    shortcuts: dict[str, str] = {}
    for key, value in selected.items():
        if not isinstance(key, str) or not _MODEL_SHORTCUT_KEY.fullmatch(key):
            raise ModelShortcutError(
                f"malformed {_MODEL_SHORTCUTS_ENV}: invalid {subagent} shortcut key"
            )
        if not isinstance(value, str) or not value.strip():
            raise ModelShortcutError(
                f"malformed {_MODEL_SHORTCUTS_ENV}: target for {key} must be a non-empty string"
            )
        shortcuts[key] = value.strip()
    return shortcuts


def resolve_model_shortcut(
    model: str,
    shipped_shortcuts: Mapping[str, str],
    subagent: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Resolve shipped and project shortcuts, refusing malformed or unknown aliases."""
    if subagent not in _MODEL_SHORTCUT_SUBAGENTS:
        raise ModelShortcutError(f"unknown model-shortcut subagent: {subagent}")
    source = os.environ if environment is None else environment
    shortcuts = {**shipped_shortcuts, **_project_model_shortcuts(subagent, source)}
    current = model
    chain: list[str] = []
    while current.startswith(":"):
        if current in chain:
            cycle = " -> ".join([*chain[chain.index(current):], current])
            raise ModelShortcutError(f"model shortcut cycle for {subagent}: {cycle}")
        target = shortcuts.get(current)
        if target is None:
            raise ModelShortcutError(f"unknown model shortcut for {subagent}: {current}")
        chain.append(current)
        current = target
    return current


def is_continuity_environment_key(name: str) -> bool:
    return name in _LEGACY_CONTINUITY_KEYS or name.startswith(_SCOPED_CONTINUITY_KEY_PREFIXES)


def sanitize_model_shortcut_environment() -> None:
    """Remove internal shortcut transport before launching a provider process."""
    for name in _MODEL_SHORTCUT_ENV_KEYS:
        os.environ.pop(name, None)


def child_process_environment(
    base: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    if overrides:
        environment.update(overrides)
    return {
        name: value
        for name, value in environment.items()
        if not is_continuity_environment_key(name)
        and name not in _MODEL_SHORTCUT_ENV_KEYS and name not in {_STARTUP_ORIGIN_KEY, _STARTUP_PROGRESS_KEY}
    }


def sanitize_current_process_environment() -> None:
    for name in tuple(os.environ):
        if is_continuity_environment_key(name) or name in {_STARTUP_ORIGIN_KEY, _STARTUP_PROGRESS_KEY}:
            os.environ.pop(name, None)
