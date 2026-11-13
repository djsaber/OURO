from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ouro.application.plugins.service import PluginService
from ouro.cli.main import main
from ouro.plugins.builtin import BuiltinPluginDefinition, BuiltinPluginRegistry
from ouro.plugins.installation.package import validate_plugin_package
from ouro.plugins.installation.scaffold import create_plugin_scaffold
from ouro.plugins.installation.store import PluginPackageStore


def _package(root: Path) -> Path:
    create_plugin_scaffold(root, plugin_id="dev.example.author", name="Author example")
    (root / "author_plugin.py").write_text("raise AssertionError('must not import')\n")
    return root


def _set_author(root: Path, author: object) -> None:
    path = root / ".ouro-plugin/plugin.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["author"] = author
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize(
    "author",
    [None, {"name": "个人作者"}, {"name": "Example Team", "url": "https://example.com"}],
)
def test_author_survives_installation_and_existing_queries(tmp_path, capsys, author):
    source = _package(tmp_path / "source")
    if author is not None:
        _set_author(source, author)
    home = tmp_path / "home"
    installed = PluginService(PluginPackageStore(home)).installer.install(source).installed
    service = PluginService(PluginPackageStore(home))
    summary = service.list()[0]
    assert summary.status == "disabled"
    detail = service.show(installed.plugin_id)
    for view in (summary, detail, detail.installs[0]):
        assert view.model_dump(exclude_none=True).get("author") == author

    options = ["--home", str(home), "--json"]
    assert main(["plugin", "list", *options]) == 0
    rows = json.loads(capsys.readouterr().out)
    row = next(row for row in rows if row["plugin_id"] == installed.plugin_id)
    assert row.get("author") == author
    assert main(["plugin", "show", installed.plugin_id, *options]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result.get("author") == author
    assert result["installs"][0].get("author") == author


async def test_author_follows_selected_package_and_each_installed_version(tmp_path):
    source = _package(tmp_path / "source")
    service = PluginService(PluginPackageStore(tmp_path / "home"))
    first = service.installer.install(source).installed
    _set_author(source, {"name": "New team"})
    second = service.installer.install(source).installed
    assert first.package_digest != second.package_digest
    assert service.list()[0].author is None
    detail = service.show(first.plugin_id)
    assert {
        item.package_digest: item.author.name if item.author else None for item in detail.installs
    } == {
        first.package_digest: None,
        second.package_digest: "New team",
    }
    await service.select(first.plugin_id, second.package_digest)
    assert service.list()[0].author.name == "New team"
    assert service.show(first.plugin_id).author.name == "New team"
    await service.select(first.plugin_id, first.package_digest)
    assert service.show(first.plugin_id).author is None


@pytest.mark.parametrize("author", [{}, {"name": ""}, {"name": " \t"}, {"name": 123}])
def test_declared_author_requires_a_nonblank_name(tmp_path, author):
    source = _package(tmp_path / "source")
    _set_author(source, author)
    with pytest.raises(ValidationError, match="author.name"):
        validate_plugin_package(source)


def test_explicit_null_author_is_optional(tmp_path):
    source = _package(tmp_path / "source")
    _set_author(source, None)
    assert validate_plugin_package(source).manifest.author is None


def test_builtin_queries_include_declared_author(tmp_path):
    source = _package(tmp_path / "source")
    _set_author(source, {"name": "Builtin team"})
    manifest = validate_plugin_package(source).manifest

    def factory(context):
        pytest.fail("query must not start builtin plugin")

    service = PluginService(
        PluginPackageStore(tmp_path / "home"),
        builtin_plugins=BuiltinPluginRegistry(
            [BuiltinPluginDefinition(manifest=manifest, root=source, factory=factory)]
        ),
    )
    assert service.list()[0].author.name == "Builtin team"
    assert service.show(manifest.plugin_id).author.name == "Builtin team"
