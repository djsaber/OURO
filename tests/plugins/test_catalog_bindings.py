from __future__ import annotations

import weakref
from dataclasses import dataclass, field, replace

import pytest

from ouro.core.base import utc_now
from ouro.core.observers import (
    ObserverRegistration,
    ObserverRegistry,
    RuntimeEventObservation,
    RuntimeEventView,
)
from ouro.core.run_drivers import DriverFinished
from ouro.plugins.catalog import ExtensionCatalogBase, GenerationGate, build_extension_catalog
from ouro.plugins.contributions import ContributionOwner, PreparedContributionSet
from ouro.plugins.models import PluginGenerationRecord
from ouro.plugins.registry import PluginCommand, PluginCommandRegistry
from ouro.plugins.run_drivers import RunDriverRegistration
from ouro.tools import ToolRegistry


@dataclass
class Generation:
    plugin_id: str
    record: PluginGenerationRecord = field(init=False)
    contributions: PreparedContributionSet = field(init=False)
    gate: GenerationGate = field(init=False)
    leases: int = 0

    def __post_init__(self):
        self.record = PluginGenerationRecord(
            generation_id=self.plugin_id,
            plugin_id=self.plugin_id,
            version="1.0.0",
            package_digest="digest",
            runtime_kind="python-inprocess",
            configuration_revision_id="a" * 64,
            dependency_lock_fingerprint="b" * 64,
            plugin_api_version="3.0.0",
            execution_contract_hash="c" * 64,
        )
        self.gate = GenerationGate(self.plugin_id)
        self.contributions = PreparedContributionSet(
            owner=ContributionOwner(
                plugin_id=self.plugin_id,
                generation_id=self.plugin_id,
                plugin_class="user",
                release_id="digest",
                package_digest="digest",
            ),
            commands=(PluginCommand(name="run", plugin_id=self.plugin_id, handler=lambda _: "ok"),),
            observers=(
                ObserverRegistration(
                    plugin_id=self.plugin_id,
                    observer_id="listen",
                    event="runtime_event",
                    event_types=("run_started",),
                    handler=lambda _: None,
                ),
            ),
        )

    def acquire(self):
        self.leases += 1
        return Lease(self)


@dataclass
class Lease:
    generation: Generation

    @property
    def record(self):
        return self.generation.record

    def close(self):
        self.generation.leases -= 1


def catalog_for(*generations, dependencies=None):
    return build_extension_catalog(
        ExtensionCatalogBase.capture(
            tools=ToolRegistry(),
            observers=ObserverRegistry(),
            commands=PluginCommandRegistry(),
            compaction=None,
            continuations=None,
            tool_policies=None,
        ),
        generations,
        dependencies=dependencies,
    )


def test_command_binding_owns_only_its_dependency_closure_and_not_the_catalog():
    owner, dependency, unrelated = (Generation(f"dev.ouro.{name}") for name in ("a", "b", "c"))
    catalog = catalog_for(
        owner, dependency, unrelated, dependencies={owner.plugin_id: (dependency.plugin_id,)}
    )
    reference = weakref.ref(catalog)
    binding = catalog.bind_command(f"{owner.plugin_id}:run")
    del catalog
    assert reference() is None
    assert [item.leases for item in (owner, dependency, unrelated)] == [1, 1, 0]
    commands, _ = binding.value
    assert [item.command_id for item in commands.list()] == [f"{owner.plugin_id}:run"]
    binding.close()
    binding.close()
    assert [item.leases for item in (owner, dependency, unrelated)] == [0, 0, 0]


def test_driver_binding_retains_only_owner_dependencies_and_requires_exact_contract():
    owner, dependency, unrelated = (Generation(f"dev.ouro.{name}") for name in ("a", "b", "c"))

    async def execute(context, resume):
        return DriverFinished("done")

    registration = RunDriverRegistration(
        "review",
        execute,
        owner.plugin_id,
        "digest",
        contract_hash="entry",
        execution_contract_hash="c" * 64,
    )
    owner.contributions = replace(owner.contributions, run_drivers=(registration,))
    catalog = catalog_for(
        owner, dependency, unrelated, dependencies={owner.plugin_id: (dependency.plugin_id,)}
    )
    with catalog.bind_driver(registration.canonical_id) as selected:
        expected = selected.value.binding
    wrong = expected.model_copy(update={"contract_hash": "other"})
    with pytest.raises(ValueError, match="contract is unavailable"):
        catalog.bind_driver(registration.canonical_id, expected=wrong)
    assert [item.leases for item in (owner, dependency, unrelated)] == [0, 0, 0]
    binding = catalog.bind_driver(registration.canonical_id, expected=expected)
    reference = weakref.ref(catalog)
    del catalog
    assert reference() is None
    assert [item.leases for item in (owner, dependency, unrelated)] == [1, 1, 0]
    binding.close()
    assert [item.leases for item in (owner, dependency, unrelated)] == [0, 0, 0]


def test_observer_binding_excludes_unmatched_plugins_and_releases_the_catalog():
    matched, unrelated = Generation("dev.ouro.a"), Generation("dev.ouro.b")

    unrelated.contributions = replace(unrelated.contributions, observers=())
    catalog = catalog_for(matched, unrelated)
    reference = weakref.ref(catalog)
    observation = RuntimeEventObservation(
        session_id="session",
        run_id="run",
        record=RuntimeEventView(
            event_id="event",
            session_id="session",
            run_id="run",
            turn_id=None,
            event_type="run_started",
            created_at=utc_now(),
            sequence=1,
        ),
    )
    binding = catalog.bind_observers(observation, active_controller_ids=lambda: set())
    del catalog
    assert reference() is None
    assert (matched.leases, unrelated.leases) == (1, 0)
    assert len(binding.value.matching(observation)) == 1
    binding.close()
    assert (matched.leases, unrelated.leases) == (0, 0)
