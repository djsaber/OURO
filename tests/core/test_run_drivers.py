import pytest
from pydantic import ValidationError

from ouro.core.errors import UnsupportedDataVersionError
from ouro.core.messages import MessageRecord
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.run_drivers import DriverCheckpoint, RunDriverBinding
from ouro.core.state import RunRecord


def test_default_driver_contract_rejects_unrecognized_implementation():
    with pytest.raises(ValidationError, match="unsupported default"):
        RunDriverBinding(
            implementation=dict(
                plugin_id="example.plugin",
                plugin_class="user",
                release_id="release",
                configuration_revision_id="config",
                execution_contract_hash="execution",
            ),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"implementation": None},
        {"driver_id": "other/run_drivers/main"},
    ],
)
def test_plugin_driver_requires_an_exact_owned_contract(changes):
    fields = dict(
        driver_id="example.plugin/run_drivers/main",
        implementation=dict(
            plugin_id="example.plugin",
            plugin_class="user",
            release_id="release",
            configuration_revision_id="configuration",
            execution_contract_hash="execution",
        ),
    )
    with pytest.raises(ValidationError, match="exact implementation contract"):
        RunDriverBinding(**(fields | changes))


def test_driver_dependency_identities_cannot_shadow_the_owner_or_each_other():
    implementation = dict(
        plugin_id="example.plugin",
        plugin_class="user",
        release_id="release",
        configuration_revision_id="configuration",
        execution_contract_hash="execution",
    )
    with pytest.raises(ValidationError, match="identities must be unique"):
        RunDriverBinding(
            driver_id="example.plugin/run_drivers/main",
            implementation=implementation,
            dependencies=(implementation,),
        )


def test_checkpoint_bound_is_utf8_bytes_and_rejects_non_json_numbers():
    DriverCheckpoint(batch_id="batch", payload="a" * (64 * 1024 - 2))
    with pytest.raises(ValidationError, match="64 KiB"):
        DriverCheckpoint(batch_id="batch", payload="中" * (64 * 1024 // 3))
    with pytest.raises(ValidationError):
        DriverCheckpoint(batch_id="batch", payload=float("nan"))


def test_default_driver_uses_the_native_checkpoint_contract():
    checkpoint = DriverCheckpoint(batch_id="batch", payload="turn")
    run = RunRecord(session_id="session", driver_checkpoint=checkpoint)
    assert run.driver.is_default
    assert run.driver_checkpoint == checkpoint


@pytest.mark.parametrize(
    ("record_type", "schema"),
    [(RunRecord, version) for version in (2, 3, 4, 5, 11)]
    + [(MessageRecord, version) for version in (2, 3, 4, 8)],
)
def test_decoder_rejects_records_without_current_driver_provenance_schema(record_type, schema):
    current = record_type.model_fields["schema_version"].default
    with pytest.raises(UnsupportedDataVersionError, match=f"expected {current}"):
        decode_versioned_record_json(
            record_type,
            '{"schema_version":' + str(schema) + ',"session_id":"session"}',
            label=record_type.__name__,
        )
