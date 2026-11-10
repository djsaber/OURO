import json

import pytest

from ouro.plugins.models import PluginStateDocument, create_configuration_revision
from ouro_upgrade.engine import upgrade
from ouro_upgrade.units import UpgradeUnit


@pytest.mark.parametrize("corrupt", [False, True])
def test_plugin_metadata_upgrade_and_discard_preserve_packages_and_private_data(tmp_path, corrupt):
    raw = {
        "schema_version": 4,
        "plugins": {
            "example.plugin": {
                "plugin_id": "example.plugin",
                "installs": [
                    {
                        "plugin_id": "example.plugin",
                        "version": "1.0.0",
                        "package_digest": "digest",
                        "manifest": {
                            "schema_version": 3,
                            "plugin_id": "example.plugin",
                            "name": "Example",
                            "version": "1.0.0",
                            "namespace": "example",
                            "engines": {"ouro": "*", "plugin_api": "*"},
                        },
                        "provenance": {
                            "source_kind": "local",
                            "source_reference": "source",
                            "public_source": "source",
                            "package_digest": "wrong" if corrupt else "digest",
                        },
                    }
                ],
                "activation": {
                    "selected_digest": "digest",
                    "active_configuration_revision_id": "revision",
                    "configuration_revision_by_digest": {"digest": "revision"},
                    "configuration_revisions": {
                        "revision": {"revision_id": "revision", "values": {"version": "private"}}
                    },
                },
            }
        },
    }
    revision = create_configuration_revision(values={"version": "private"}, credential_refs={})
    activation = raw["plugins"]["example.plugin"]["activation"]
    activation["active_configuration_revision_id"] = revision.revision_id
    activation["configuration_revision_by_digest"] = {"digest": revision.revision_id}
    activation["configuration_revisions"] = {revision.revision_id: revision.model_dump(mode="json")}
    path = tmp_path / "plugins/state.json"
    path.parent.mkdir()
    path.write_text(json.dumps(raw), "utf-8")
    retained = []
    for name in ("store/package/file", "data/plugin/private", "audit.jsonl"):
        item = path.parent / name
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_bytes(b"preserved")
        retained.append(item)
    original = path.read_bytes()
    report = upgrade(tmp_path, check=True, isolate_failures=True)
    assert path.read_bytes() == original
    if corrupt:
        assert report["failures"][0]["unit"]["identity"] == "plugins/state.json"
        result = upgrade(
            tmp_path,
            isolate_failures=True,
            discard=frozenset({UpgradeUnit("configuration", "plugins/state.json")}),
        )
        assert not path.exists()
        assert (tmp_path / result["backup"] / "0").read_bytes() == original
    else:
        assert report["status"] == "upgrade_required"
        upgrade(tmp_path)
        converted = json.loads(path.read_bytes())
        record = PluginStateDocument.model_validate(converted).plugins["example.plugin"]
        assert record.selected().plugin_id == "example.plugin"
        assert record.activation.configuration_for("digest").values == {"version": "private"}
        assert "plugin_id" not in converted["plugins"]["example.plugin"]["installs"][0]
        assert upgrade(tmp_path)["status"] == "current"
    assert all(item.read_bytes() == b"preserved" for item in retained)
