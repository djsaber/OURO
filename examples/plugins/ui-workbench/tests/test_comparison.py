from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

import pytest
from ui_workbench_plugin.comparison import build_comparison
from ui_workbench_plugin.files import WorkbenchError


class _Tags(HTMLParser):
    def __init__(self, markup: str) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.feed(markup)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def get(self, tag: str) -> list[dict[str, str | None]]:
        return [attrs for name, attrs in self.tags if name == tag]

    def assert_offline_policy(self) -> None:
        policies = [
            attrs["content"]
            for attrs in self.get("meta")
            if attrs.get("http-equiv", "") == "Content-Security-Policy"
        ]
        assert len(policies) == 1 and policies[0] is not None
        directives = dict(directive.strip().split(" ", 1) for directive in policies[0].split(";"))
        assert directives == {
            "default-src": "'none'",
            "style-src": "'unsafe-inline'",
            "img-src": "data:",
            "base-uri": "'none'",
            "form-action": "'none'",
        }


@pytest.fixture
def board(tmp_path: Path) -> tuple[Path, dict]:
    workspace = tmp_path / "workspace"
    (workspace / "review").mkdir(parents=True)
    (workspace / "review" / "layout.htm").write_text("<h1>First draft</h1>", encoding="utf-8")
    (workspace / "direction.HTML").write_text("<h1>Second draft</h1>", encoding="utf-8")
    return workspace, {
        "title": "Design & direction",
        "brief": "Compare both approaches.\nPay attention to hierarchy.",
        "candidates": [
            {"title": "Hierarchy", "path": "layout.htm", "summary": "Lead with the product."},
            {"title": "Palette", "path": "../direction.HTML"},
        ],
    }


def _manifest(workspace: Path, document: object) -> str:
    path = "review/board.json"
    (workspace / path).write_text(json.dumps(document), encoding="utf-8")
    return path


def test_builds_ordered_offline_board_and_resolves_from_manifest(board: tuple[Path, dict]) -> None:
    workspace, document = board
    path = _manifest(workspace, document)
    result = build_comparison(workspace, path)
    assert result == build_comparison(workspace, path)
    markup = result.decode("utf-8")
    assert "Design &amp; direction" in markup
    assert document["brief"] in markup
    tags = _Tags(markup)
    tags.assert_offline_policy()
    assert not tags.get("script")
    assert [anchor["href"] for anchor in tags.get("a")] == [
        "#review",
        "#direction-1",
        "#direction-2",
    ]
    assert tags.get("link")
    assert all((link.get("href") or "").startswith("data:") for link in tags.get("link"))
    frames = tags.get("iframe")
    assert [frame["title"] for frame in frames] == [
        "Direction 01 preview: Hierarchy",
        "Direction 02 preview: Palette",
    ]
    assert [frame["aria-describedby"] for frame in frames] == [
        "preview-note summary-1",
        "preview-note",
    ]
    for frame, heading in zip(frames, ("First draft", "Second draft"), strict=True):
        assert frame["sandbox"] == ""
        assert frame["referrerpolicy"] == "no-referrer"
        assert frame["tabindex"] == "0"
        assert "src" not in frame
        srcdoc = frame["srcdoc"]
        assert srcdoc is not None and f"<h1>{heading}</h1>" in srcdoc
        _Tags(srcdoc).assert_offline_policy()


def test_six_candidates_and_absent_optional_fields(board: tuple[Path, dict]) -> None:
    workspace, document = board
    document = {"title": "Round two", "candidates": []}
    for index in range(6):
        path = f"draft-{index}.html"
        (workspace / "review" / path).write_text(f"<p>Draft {index}</p>", encoding="utf-8")
        document["candidates"].append({"title": f"Draft {index}", "path": path})
    markup = build_comparison(workspace, _manifest(workspace, document)).decode("utf-8")
    assert len(_Tags(markup).get("iframe")) == 6
    assert 'class="brief"' not in markup
    assert 'class="summary"' not in markup


def test_escapes_metadata_and_limits_candidates_to_static_content(board: tuple[Path, dict]) -> None:
    workspace, document = board
    label = '$Title "<img src=x>" & details'
    document["title"] = label
    document["brief"] = label
    document["candidates"][0]["title"] = label
    document["candidates"][0]["summary"] = label
    data_image = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    source = f"""
        <html lang="fr"><head><title>Ignored</title><base href="https://example.invalid/">
          <meta http-equiv="refresh" content="1;url=https://example.invalid/">
          <link rel="stylesheet" href="https://example.invalid/linked.css">
          <style>@import url(https://example.invalid/theme.css);
            .card {{ color: rebeccapurple; background: url(./background.png); }}</style>
        </head><body class="card" onload="ignore()">
        <script>ignore()</script>
        <p style="font-weight:600" contenteditable>&lt;em&gt;A &amp; B&lt;/em&gt;</p>
        <a href="https://example.invalid/" target="_blank" ping="/log">Link label</a>
        <form action="/saved"><button formaction="/other" autofocus>Go</button></form>
        <img src="{data_image}" srcset="/x.png" onerror="ignore()" alt="An image">
        <img src="./local.png" alt="Local asset">
        <img src="https://example.invalid/a.png" alt="Remote asset">
        <svg><text>Ignored SVG</text></svg>
        <iframe src="https://example.invalid/">Ignored frame</iframe>
        </body></html>
    """
    (workspace / "review" / "layout.htm").write_text(source, encoding="utf-8")
    output = build_comparison(workspace, _manifest(workspace, document)).decode("utf-8")
    outer = _Tags(output)
    assert not outer.get("img")
    preview = outer.get("iframe")[0]
    assert preview["title"] == f"Direction 01 preview: {label}"
    srcdoc = preview["srcdoc"]
    assert srcdoc is not None
    inner = _Tags(srcdoc)
    inner.assert_offline_policy()
    assert not any(inner.get(tag) for tag in ("script", "svg", "link", "iframe", "base"))
    assert len(inner.get("meta")) == 4
    assert inner.get("html") == [{"lang": "fr"}]
    assert inner.get("body") == [{"class": "card"}]
    assert inner.get("p") == [{"style": "font-weight:600"}]
    assert inner.get("a") == [{}]
    assert inner.get("form") == [{}]
    assert inner.get("button") == [{"disabled": None}]
    assert inner.get("img") == [
        {"src": data_image, "alt": "An image"},
        {"alt": "Local asset"},
        {"alt": "Remote asset"},
    ]
    assert "&lt;em&gt;A &amp; B&lt;/em&gt;" in srcdoc
    assert "color: rebeccapurple" in srcdoc
    assert "Ignored" not in srcdoc


@pytest.mark.parametrize(
    ("update", "error"),
    [
        ({"title": "   "}, "title"),
        ({"title": "bad\ud800"}, "title"),
        ({"brief": None}, "brief"),
        ({"brief": "x" * 2001}, "brief"),
        ({"typo": "unknown field"}, "only these fields"),
        ({"candidates": []}, "2–6"),
        ({"candidates": [{}] * 7}, "2–6"),
        ({"candidates": [None, {}]}, "Candidate 1 must be an object"),
        ({"candidates": [{"title": "Candidate 1", "path": 12}, {}]}, "path"),
    ],
)
def test_rejects_invalid_manifest_schema(
    board: tuple[Path, dict],
    update: dict,
    error: str,
) -> None:
    workspace, document = board
    with pytest.raises(WorkbenchError, match=error):
        build_comparison(workspace, _manifest(workspace, document | update))


@pytest.mark.parametrize(
    ("source", "error"),
    [
        (b"{bad JSON", "valid JSON"),
        (b'{"title":"A","title":"B"}', "repeat JSON field names"),
        (b"\xff", "UTF-8"),
        pytest.param(b" " * 65537, "size limit", id="manifest-byte-limit"),
    ],
)
def test_rejects_bad_or_oversized_manifest_bytes(
    board: tuple[Path, dict],
    source: bytes,
    error: str,
) -> None:
    workspace, _ = board
    path = "review/board.json"
    (workspace / path).write_bytes(source)
    with pytest.raises(WorkbenchError, match=error):
        build_comparison(workspace, path)


@pytest.mark.parametrize(
    ("content", "error"),
    [
        (b"\xff", "UTF-8"),
        (b"<script></script>", "no supported static"),
        pytest.param(b"a" * (1024 * 1024 + 1), "size limit", id="candidate-byte-limit"),
    ],
)
def test_rejects_unsupported_candidate_bytes(
    board: tuple[Path, dict],
    content: bytes,
    error: str,
) -> None:
    workspace, document = board
    (workspace / "review" / "layout.htm").write_bytes(content)
    with pytest.raises(WorkbenchError, match=error):
        build_comparison(workspace, _manifest(workspace, document))


def test_distinct_candidate_files_can_share_a_static_preview(board: tuple[Path, dict]) -> None:
    workspace, document = board
    document["candidates"][1]["path"] = "./layout.htm"
    with pytest.raises(WorkbenchError, match="repeats a candidate file"):
        build_comparison(workspace, _manifest(workspace, document))

    document["candidates"][1]["path"] = "../direction.HTML"
    (workspace / "direction.HTML").write_text("<!--Copy--><h1>First draft</h1>", encoding="utf-8")
    markup = build_comparison(workspace, _manifest(workspace, document)).decode("utf-8")
    frames = _Tags(markup).get("iframe")
    assert len(frames) == 2
    assert frames[0]["srcdoc"] == frames[1]["srcdoc"]
    assert frames[0]["title"] != frames[1]["title"]


def test_rejects_boundary_crossing_for_manifest_and_candidate(board: tuple[Path, dict]) -> None:
    workspace, document = board
    path = _manifest(workspace, document)
    (workspace.parent / "outside.html").write_text("<h1>Outside</h1>", encoding="utf-8")
    (workspace.parent / "outside.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(WorkbenchError, match="inside the current workspace"):
        build_comparison(workspace, "../outside.json")

    document["candidates"][1]["path"] = "../../outside.html"
    _manifest(workspace, document)
    with pytest.raises(WorkbenchError, match="inside the current workspace"):
        build_comparison(workspace, path)


def test_rejects_candidate_symlink_to_outside_workspace(board: tuple[Path, dict]) -> None:
    workspace, document = board
    outside = workspace.parent / "outside.html"
    outside.write_text("<h1>Outside</h1>", encoding="utf-8")
    try:
        (workspace / "review" / "alias.html").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Cannot create symlinks on this system: {exc}")
    document["candidates"][1]["path"] = "alias.html"
    with pytest.raises(WorkbenchError, match="inside the current workspace"):
        build_comparison(workspace, _manifest(workspace, document))
