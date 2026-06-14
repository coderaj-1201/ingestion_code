"""
Lightweight stub for `agent_framework` so the test suite can import agent
modules without the proprietary MAF package installed.

Registered as a plugin via conftest.py (no action needed — pytest auto-discovers
conftest files). The stub is installed into sys.modules before any test module
imports agent code.
"""
from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock


def _install_agent_framework_stub():
    """Insert a minimal agent_framework stub into sys.modules."""
    if "agent_framework" in sys.modules:
        return

    stub = ModuleType("agent_framework")

    # @step — identity decorator; just returns the coroutine unchanged
    def step(fn):
        return fn

    # Workflow result returned by workflow.run(...)
    class _WorkflowResult:
        def __init__(self, output):
            self._output = output

        def get_outputs(self):
            return [self._output] if self._output is not None else []

    # @workflow — wraps an async function so workflow.run(args) can be awaited
    class _WorkflowDecorator:
        def __init__(self, fn, name=""):
            self._fn = fn
            self.name = name

        async def run(self, *args, **kwargs):
            result = await self._fn(*args, **kwargs)
            return _WorkflowResult(result)

        # Make the decorator itself callable (so @workflow(name=...) works)
        def __call__(self, *args, **kwargs):
            return self._fn(*args, **kwargs)

    def workflow(name=""):
        def decorator(fn):
            return _WorkflowDecorator(fn, name=name)
        return decorator

    stub.step = step
    stub.workflow = workflow
    sys.modules["agent_framework"] = stub


_install_agent_framework_stub()


def _install_azure_stubs():
    """
    Stub out Azure SDK modules whose native extensions are broken in this
    sandbox (cryptography Rust bindings, cffi). All real calls are mocked
    in individual tests anyway — these stubs only satisfy import-time lookups.
    """
    import sys
    from types import ModuleType
    from unittest.mock import MagicMock

    _STUB_PREFIXES = [
        "cryptography",
        "azure.storage.blob",
        "azure.storage.blob.aio",
        "azure.servicebus",
        "azure.servicebus.aio",
        "azure.identity",
        "azure.identity.aio",
        "azure.core.credentials",
        "azure.core.exceptions",
        "azure.ai.projects",
        "azure.monitor.opentelemetry",
        "azure.search.documents",
        "azure.search.documents.aio",
        "azure.search.documents.indexes",
        "azure.search.documents.models",
        "pdfplumber",
        "pymupdf",
        "fitz",
    ]

    for name in _STUB_PREFIXES:
        if name not in sys.modules:
            sys.modules[name] = MagicMock()

    # Ensure sub-modules are also stubs so `from azure.storage.blob.aio import X` works
    for name in list(sys.modules):
        if any(name.startswith(p) for p in _STUB_PREFIXES):
            pass  # already there


_install_azure_stubs()


def _set_test_env():
    """
    Set required environment variables before any agent module imports Settings.
    pydantic-settings reads from os.environ at construction time and Settings
    is cached with lru_cache, so values must be present before the first import.
    """
    import os
    os.environ.setdefault("AZURE_FOUNDRY_PROJECT_ENDPOINT",    "https://my-foundry.api.azureml.ms")
    os.environ.setdefault("AZURE_STORAGE_ACCOUNT_NAME",        "mystorageaccount")
    os.environ.setdefault("AZURE_SEARCH_ENDPOINT",             "https://my-search.search.windows.net")
    os.environ.setdefault("AZURE_SEARCH_API_KEY",              "fake-search-api-key-32-chars-min")
    os.environ.setdefault("AZURE_SERVICE_BUS_NAMESPACE",       "my-sb.servicebus.windows.net")
    os.environ.setdefault("SHAREPOINT_TENANT_ID",              "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    os.environ.setdefault("SHAREPOINT_CLIENT_ID",              "11111111-2222-3333-4444-555555555555")
    os.environ.setdefault("SHAREPOINT_CLIENT_SECRET",          "super-secret-sharepoint-value")
    os.environ.setdefault("SHAREPOINT_WEBHOOK_SECRET",         "somesecret-32chars-minimum-value")
    os.environ.setdefault("LOGIC_APP_WEBHOOK_SECRET",          "logic-app-test-secret-32chars-ok")


_set_test_env()
