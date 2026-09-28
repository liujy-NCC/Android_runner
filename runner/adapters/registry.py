"""Static component registry keyed by ``(component type, name, version)``.

DESIGN.md §4.1: v1 resolves implementations from a static, researcher-owned
registry. The Agent cannot install components or load Runner plugins from a
candidate source directory — there is no code path that reads a plugin path
from a request.
"""

from __future__ import annotations

from typing import Any, Callable

from runner.errors import CapabilityError

_REGISTRY: dict[tuple[str, str, str], Callable[..., Any]] = {}


def register(kind: str, name: str, version: str, factory: Callable[..., Any]) -> None:
    _REGISTRY[(kind, name, version)] = factory


def resolve(kind: str, name: str, version: str | None = None, **kwargs: Any) -> Any:
    if version is not None:
        factory = _REGISTRY.get((kind, name, version))
        if factory is None:
            raise CapabilityError(f"{kind} not registered: {name}@{version}")
        return factory(**kwargs)
    for (k, n, _v), factory in sorted(_REGISTRY.items()):
        if k == kind and n == name:
            return factory(**kwargs)
    raise CapabilityError(f"{kind} not registered: {name}")


def registered() -> list[tuple[str, str, str]]:
    return sorted(_REGISTRY)


def _install_defaults() -> None:
    from runner.adapters.build_backend import MockBuildBackend, NotIntegratedBuildBackend
    from runner.adapters.collectors_mock import NotIntegratedCollector
    from runner.adapters.device_mock import MockDeviceAdapter
    from runner.adapters.oracle import MockOracle, NotIntegratedOracle
    from runner.adapters.projection import DiagnosticOnlyProjector

    register("build_backend", "mock", "1", MockBuildBackend)
    register("build_backend", "and000008-template", "0", NotIntegratedBuildBackend)
    register("device", "mock", "1", MockDeviceAdapter)
    register("collector", "mock", "1", lambda name="mock", required_by_default=True: NotIntegratedCollector(name))
    register("oracle", "mock", "1", MockOracle)
    register("oracle", "and000008-readonly", "0", NotIntegratedOracle)
    register("feedback_projector", "diagnostic-only-v1", "1", DiagnosticOnlyProjector)


_install_defaults()
