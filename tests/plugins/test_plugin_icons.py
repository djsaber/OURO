from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from PIL import Image

from ouro.application.plugins.service import PluginService
from ouro.cli.main import main
from ouro.plugins.builtin import BuiltinPluginDefinition, BuiltinPluginRegistry
from ouro.plugins.installation.package import validate_plugin_package
from ouro.plugins.installation.paths import PluginPackageError
from ouro.plugins.installation.store import PluginPackageStore


def _package(root: Path, icon: str | None = "./assets/icon.png") -> Path:
    (root / ".ouro-plugin").mkdir(parents=True)
    manifest = {
        "plugin_id": "dev.example.icon",
        "name": "Icon example",
        "version": "1.0.0",
        "namespace": "icon",
        "engines": {"ouro": ">=0.4.0", "plugin_api": "^5.0.0"},
        "runtime": {"kind": "python-inprocess", "entrypoint": "icon_plugin:create_plugin"},
    }
    if icon is not None:
        manifest["icon"] = icon
    (root / ".ouro-plugin/plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "icon_plugin.py").write_text("raise AssertionError('must not import')\n")
    (root / "assets").mkdir()
    Image.new("RGBA", (256, 256), "red").save(root / "assets/icon.png")
    return root


def _data_url(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


async def test_queries_embed_icons_for_selected_and_candidate_packages(tmp_path):
    source = _package(tmp_path / "source")
    service = PluginService(PluginPackageStore(tmp_path / "home"))
    first_icon = _data_url(source / "assets/icon.png")
    first = service.installer.install(source).installed
    Image.new("RGBA", (256, 256), "blue").save(source / "assets/icon.png")
    second_icon = _data_url(source / "assets/icon.png")
    second = service.installer.install(source).installed
    assert first.package_digest != second.package_digest

    # Reload state through a new Host query service; no runtime or plugin import is needed.
    service = PluginService(PluginPackageStore(tmp_path / "home"))
    summary = service.list()[0]
    assert summary.icon == first_icon
    assert summary.status == "disabled"
    detail = service.show(first.plugin_id)
    assert json.loads(detail.model_dump_json())["icon"] == first_icon
    assert {item.package_digest: item.icon for item in detail.installs} == {
        first.package_digest: first_icon,
        second.package_digest: second_icon,
    }
    await service.select(first.plugin_id, second.package_digest)
    assert service.list()[0].icon == second_icon
    await service.select(first.plugin_id, first.package_digest)
    assert service.show(first.plugin_id).icon == first_icon


def test_undeclared_icon_is_not_discovered(tmp_path):
    source = _package(tmp_path / "source", icon=None)
    service = PluginService(PluginPackageStore(tmp_path / "home"))
    installed = service.installer.install(source).installed
    assert service.list()[0].icon is None
    detail = service.show(installed.plugin_id)
    assert detail.icon is None
    assert detail.installs[0].icon is None


def test_cli_queries_serialize_the_packaged_icon(tmp_path, capsys):
    source = _package(tmp_path / "source")
    home = tmp_path / "home"
    options = ["--home", str(home), "--json"]
    assert main(["plugin", "install", str(source), *options]) == 0
    capsys.readouterr()
    assert main(["plugin", "list", *options]) == 0
    rows = json.loads(capsys.readouterr().out)
    plugin = next(row for row in rows if row["plugin_id"] == "dev.example.icon")
    assert plugin["icon"] == _data_url(source / "assets/icon.png")
    assert main(["plugin", "show", "dev.example.icon", *options]) == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["icon"] == plugin["icon"] == detail["installs"][0]["icon"]


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_unavailable_installed_icon_does_not_break_queries(tmp_path, damage):
    source = _package(tmp_path / "source")
    service = PluginService(PluginPackageStore(tmp_path / "home"))
    installed = service.installer.install(source).installed
    icon = (
        service.store.package_path(installed.plugin_id, installed.package_digest)
        / "assets/icon.png"
    )
    if damage == "missing":
        icon.unlink()
    else:
        icon.write_bytes(b"broken")
    assert service.list()[0].icon is None
    detail = service.show(installed.plugin_id)
    assert detail.icon is None
    assert detail.installs[0].icon is None


@pytest.mark.parametrize(
    "icon",
    [
        "../outside.png",
        "assets/icon.png",
        "/icon.png",
        "https://example.com/icon.png",
        "./missing.png",
    ],
)
def test_invalid_declared_icon_paths_are_rejected(tmp_path, icon):
    source = _package(tmp_path / "source", icon=icon)
    with pytest.raises(PluginPackageError):
        validate_plugin_package(source)


@pytest.mark.parametrize("kind", ["corrupt", "jpeg", "bytes"])
def test_unavailable_icon_content_does_not_block_installation(tmp_path, kind):
    source = _package(tmp_path / "source")
    path = source / "assets/icon.png"
    if kind == "corrupt":
        path.write_bytes(b"not an image")
    elif kind == "jpeg":
        Image.new("RGB", (32, 32)).save(path, format="JPEG")
    else:
        path.write_bytes(path.read_bytes() + b"x" * (512 * 1024))
    service = PluginService(PluginPackageStore(tmp_path / "home"))
    installed = service.installer.install(source).installed
    assert service.list()[0].icon is None
    detail = service.show(installed.plugin_id)
    assert detail.icon is None
    assert detail.installs[0].icon is None


@pytest.mark.parametrize("kind", ["animated", "dimensions", "decode_failure"])
def test_icon_presentation_is_left_to_the_client(tmp_path, monkeypatch, kind):
    source = _package(tmp_path / "source")
    path = source / "assets/icon.png"
    if kind == "animated":
        Image.new("RGBA", (32, 32), "red").save(
            path, save_all=True, append_images=[Image.new("RGBA", (32, 32), "blue")]
        )
    elif kind == "dimensions":
        Image.new("RGBA", (1025, 1)).save(path)
    else:
        # A PNG signature does not guarantee that the client can decode the image.
        path.write_bytes(b"\x89PNG\r\n\x1a\n")

    def unexpected_decode(*args, **kwargs):
        pytest.fail("installation and queries must not decode the icon")

    monkeypatch.setattr(Image, "open", unexpected_decode)
    service = PluginService(PluginPackageStore(tmp_path / "home"))
    installed = service.installer.install(source).installed
    assert service.list()[0].icon == _data_url(path)
    assert service.show(installed.plugin_id).icon == _data_url(path)


def test_builtin_queries_include_icon_without_calling_factory(tmp_path):
    source = _package(tmp_path / "source")
    manifest = validate_plugin_package(source).manifest

    def factory(context):
        pytest.fail("query must not start builtin plugin")

    service = PluginService(
        PluginPackageStore(tmp_path / "home"),
        builtin_plugins=BuiltinPluginRegistry(
            [BuiltinPluginDefinition(manifest=manifest, root=source, factory=factory)]
        ),
    )
    assert service.list()[0].icon == _data_url(source / "assets/icon.png")
    assert service.show(manifest.plugin_id).icon == service.list()[0].icon
