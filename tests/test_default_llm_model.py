"""The scoring engine's default LLM must honour DEEPSEEK_MODEL.

Regression guard: ``build_llm`` used to default to the hardcoded premium model,
so every engine verdict silently spent money on the most expensive tier.
"""

from __future__ import annotations

import sys
import types

import pytest

from backend import llm_service
from backend.config import DEFAULT_MODEL


@pytest.fixture
def captured_kwargs(monkeypatch):
    captured: dict = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = types.ModuleType("langchain_openai")
    module.ChatOpenAI = FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", module)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    return captured


def test_configured_model_is_used_when_unspecified(captured_kwargs, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")

    llm_service.build_llm()

    assert captured_kwargs["model"] == "deepseek-flash"


def test_explicit_model_still_wins(captured_kwargs, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")

    llm_service.build_llm(model=DEFAULT_MODEL)

    assert captured_kwargs["model"] == DEFAULT_MODEL


def test_unknown_model_falls_back_to_default(captured_kwargs, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MODEL", "gpt-not-a-model")

    llm_service.build_llm()

    assert captured_kwargs["model"] == DEFAULT_MODEL


def test_engine_default_llm_is_built_from_config(captured_kwargs, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")
    monkeypatch.setattr(llm_service, "_default_llm", None)

    llm_service.get_default_llm()

    assert captured_kwargs["model"] == "deepseek-flash"
