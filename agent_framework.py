"""
Lightweight production stub for the agent_framework (MAF) package.

The real MAF package provides distributed tracing, retries, and workflow
orchestration. In this deployment we use the Azure SDK's own retry policies
and Service Bus dead-lettering, so the decorators are no-ops:

  @step     — identity; returns the function unchanged
  @workflow — wraps the function in a thin object that exposes .run()
              (mirrors the real API surface used in tests)

If the real MAF package becomes available as a pip dependency, add it to
requirements.txt and delete this file — it will be shadowed automatically.
"""
from __future__ import annotations


def step(fn):
    """Identity decorator — marks a function as an atomic MAF step."""
    return fn


class _WorkflowResult:
    def __init__(self, output):
        self._output = output

    def get_outputs(self):
        return [self._output] if self._output is not None else []


class _Workflow:
    def __init__(self, fn, name: str = ""):
        self._fn = fn
        self.name = name

    async def run(self, *args, **kwargs):
        result = await self._fn(*args, **kwargs)
        return _WorkflowResult(result)

    def __call__(self, *args, **kwargs):
        return self._fn(*args, **kwargs)


def workflow(name: str = ""):
    """Decorator that marks a coroutine as a MAF workflow entry point."""
    def decorator(fn):
        return _Workflow(fn, name=name)
    return decorator
