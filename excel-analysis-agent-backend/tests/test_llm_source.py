"""Unit tests for agent.py's LLM_SOURCE provider switch - no network.

The switch exists so the same agent can run against api.openai.com or an
Azure OpenAI deployment with an .env change and nothing else. The subtle
part, and the reason for the provider-key test below, is that deepagents
keys its harness profile (which tools and middleware to exclude) by the
provider it infers from the model object - and AzureChatOpenAI reports
"azure" where ChatOpenAI reports "openai". Registering that profile against
a hardcoded "openai" would silently do nothing on Azure: the eight excluded
deepagents tools would come back, and the default SummarizationMiddleware
would stop being excluded and run alongside this project's own low-trigger
replacement.
"""

import importlib

import pytest
from deepagents._models import get_model_provider
from langchain_openai import AzureChatOpenAI, ChatOpenAI

PROVIDER_VARS = (
    "LLM_SOURCE", "OPENAI_MODEL", "AZURE_API_BASE",
    "AZURE_DEPLOYMENT", "AZURE_API_VERSION", "AZURE_API_KEY",
)

AZURE_ENV = {
    "LLM_SOURCE": "azure",
    "AZURE_API_BASE": "https://example.openai.azure.com/",
    "AZURE_DEPLOYMENT": "gpt-5",
    "AZURE_API_VERSION": "2025-04-01-preview",
    "AZURE_API_KEY": "fake-key-for-tests",
}


def _reload_agent(monkeypatch, env):
    """agent.py builds its model at import time, so the switch is exercised
    by reloading the module under a given environment.

    load_dotenv is stubbed out for the reload: agent.py calls it at module
    level, so without this it would read the developer's real .env back
    into os.environ and undo the variables this helper just cleared - the
    test would then silently exercise whatever provider that .env happens
    to name rather than the one it asked for. (Caught exactly that way,
    once a real Azure block was added to .env.)"""
    for name in PROVIDER_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    import agent
    return importlib.reload(agent)


@pytest.fixture(autouse=True)
def _restore_agent_afterwards():
    """Leave the module as the rest of the suite expects to find it - with
    the real .env applied, since monkeypatch has by then undone the stub."""
    yield
    import agent
    importlib.reload(agent)


def test_defaults_to_openai_when_unset(monkeypatch):
    a = _reload_agent(monkeypatch, {})
    assert a.LLM_SOURCE == "openai"
    assert isinstance(a.model, ChatOpenAI)


def test_openai_source_uses_chatopenai(monkeypatch):
    a = _reload_agent(monkeypatch, {"LLM_SOURCE": "openai", "OPENAI_MODEL": "gpt-5-mini"})
    assert isinstance(a.model, ChatOpenAI)
    assert a.model.model_name == "gpt-5-mini"


def test_azure_source_uses_azurechatopenai_with_the_given_deployment(monkeypatch):
    a = _reload_agent(monkeypatch, AZURE_ENV)
    assert isinstance(a.model, AzureChatOpenAI)
    assert a.model.deployment_name == "gpt-5"
    assert a.model.azure_endpoint == "https://example.openai.azure.com/"
    assert a.model.openai_api_version == "2025-04-01-preview"


def test_both_providers_keep_the_same_generation_settings(monkeypatch):
    """Switching provider must not quietly change how the model generates -
    temperature stays absent (gpt-5 rejects it) and seed stays fixed."""
    for env in ({"LLM_SOURCE": "openai"}, AZURE_ENV):
        a = _reload_agent(monkeypatch, env)
        assert a.model.temperature is None
        assert a.model.seed == 42
        assert a.model.reasoning_effort == "medium"


def test_harness_profile_is_keyed_to_the_real_provider(monkeypatch):
    """The bug this guards: "azure" != "openai", so a hardcoded key would
    register a profile that never matches."""
    a = _reload_agent(monkeypatch, {"LLM_SOURCE": "openai"})
    assert get_model_provider(a.model) == "openai"
    a = _reload_agent(monkeypatch, AZURE_ENV)
    assert get_model_provider(a.model) == "azure"


def test_azure_without_its_env_vars_names_every_missing_one(monkeypatch):
    with pytest.raises(ValueError) as excinfo:
        _reload_agent(monkeypatch, {"LLM_SOURCE": "azure"})
    message = str(excinfo.value)
    for name in ("AZURE_API_BASE", "AZURE_DEPLOYMENT", "AZURE_API_VERSION", "AZURE_API_KEY"):
        assert name in message, f"{name} should be named in the error"


def test_unknown_source_is_rejected(monkeypatch):
    with pytest.raises(ValueError, match="not one of"):
        _reload_agent(monkeypatch, {"LLM_SOURCE": "anthropic"})
