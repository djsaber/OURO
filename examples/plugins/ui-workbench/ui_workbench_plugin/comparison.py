"""Build a self-contained, read-only review board from static HTML directions."""

from __future__ import annotations

import json
import re
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from string import Template

from .files import WorkbenchError, read_limited, workspace_file

_MANIFEST_LIMIT = 64 * 1024
_CANDIDATE_LIMIT = 1024 * 1024
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "base-uri 'none'; form-action 'none'"
)

_ELEMENTS = frozenset(
    """
    a abbr address article aside b bdi bdo blockquote br button caption cite code col
    colgroup dd del dfn div dl dt em fieldset figcaption figure footer form h1 h2 h3
    h4 h5 h6 header hr i img input ins kbd label legend li main mark meter nav ol
    optgroup option p pre progress q rp rt ruby s samp section select small span
    strong sub sup table tbody td textarea tfoot th thead time tr u ul var wbr
""".split()
)
_VOID = frozenset({"br", "col", "hr", "img", "input", "wbr"})
_CONTROLS = frozenset({"button", "input", "select", "textarea"})
_DROP_CONTENT = frozenset(
    {
        "iframe",
        "math",
        "noscript",
        "object",
        "script",
        "svg",
        "template",
        "title",
    }
)
_GLOBAL_ATTRS = frozenset(
    {
        "aria-describedby",
        "aria-hidden",
        "aria-label",
        "aria-labelledby",
        "class",
        "dir",
        "hidden",
        "id",
        "lang",
        "role",
        "style",
        "title",
    }
)
_ELEMENT_ATTRS = {
    "col": {"span"},
    "colgroup": {"span"},
    "img": {"alt", "height", "width"},
    "input": {"checked", "placeholder", "size", "type", "value"},
    "label": {"for"},
    "meter": {"high", "low", "max", "min", "optimum", "value"},
    "ol": {"reversed", "start", "type"},
    "optgroup": {"label"},
    "option": {"label", "selected"},
    "progress": {"max", "value"},
    "select": {"multiple", "size"},
    "td": {"colspan", "rowspan"},
    "textarea": {"cols", "placeholder", "rows"},
    "th": {"colspan", "rowspan", "scope"},
    "time": {"datetime"},
}
_RASTER = re.compile(r"data:image/(?:png|jpeg|gif|webp|avif);base64,[a-z0-9+/]+=*", re.I | re.ASCII)
_INVALID_TEXT = re.compile(r"[\x00\ud800-\udfff]")


def build_comparison(workspace: Path, manifest_path: str) -> bytes:
    """Render a UTF-8 HTML board; reads only the manifest and its declared candidates.

    The manifest is a UTF-8 JSON object (up to 64 KiB) with a nonblank ``title``,
    optional ``brief``, and 2–6 ordered ``candidates``. Each candidate has a nonblank
    ``title`` and ``path`` and optional ``summary``. Paths are relative to the
    resolved manifest's directory and must resolve inside the workspace. Candidate
    files are UTF-8 .html/.htm (up to 1 MiB each). Each candidate names a distinct file.
    Invalid input raises ``WorkbenchError``.

    Candidate previews support static HTML, inline CSS, and embedded data images
    (raster base64 in img elements). Linked/local assets are not loaded or inlined.
    Scripts, navigation, editable controls, SVG markup and embeds are not supported.
    """
    manifest_file = workspace_file(workspace, manifest_path)
    source = _read_utf8(manifest_file, _MANIFEST_LIMIT, "The comparison manifest")
    try:
        manifest = json.loads(source, object_pairs_hook=_unique_fields)
    except WorkbenchError:
        raise
    except (ValueError, RecursionError) as exc:
        raise WorkbenchError("The comparison manifest must be valid JSON.") from exc

    manifest = _object(manifest, {"title", "brief", "candidates"}, "The comparison manifest")
    title = escape(_text(manifest, "title", 120, "The comparison manifest", required=True))
    brief = escape(_text(manifest, "brief", 2000, "The comparison manifest"))
    candidates = manifest.get("candidates")
    if not isinstance(candidates, list) or not 2 <= len(candidates) <= 6:
        raise WorkbenchError("The comparison manifest must contain 2–6 candidates.")

    seen_paths: set[Path] = set()
    jumps, cards = [], []
    for index, entry in enumerate(candidates, start=1):
        context = f"Candidate {index}"
        entry = _object(entry, {"title", "path", "summary"}, context)
        name = escape(_text(entry, "title", 120, context, required=True))
        summary = escape(_text(entry, "summary", 1000, context))
        relative = _text(entry, "path", 1024, context, required=True, trim=False)
        path = workspace_file(workspace, relative, base=manifest_file.parent)
        if path.suffix.lower() not in {".html", ".htm"}:
            raise WorkbenchError(f"{context} must point to an .html or .htm file.")
        if path in seen_paths:
            raise WorkbenchError(f"{context} repeats a candidate file; use distinct HTML files.")
        seen_paths.add(path)
        preview = _preview(_read_utf8(path, _CANDIDATE_LIMIT, context), context)
        jumps.append(
            f'<li><a href="#direction-{index}"><span aria-hidden="true">{index:02}</span>'
            f"{name}</a></li>"
        )
        cards.append(_card(index, name, summary, escape(preview)))

    template = Template(Path(__file__).with_name("comparison.html").read_text(encoding="utf-8"))
    return template.substitute(
        title=title,
        brief=f'<p class="brief" dir="auto">{brief}</p>' if brief else "",
        count=f"{len(candidates):02}",
        jumps="\n".join(jumps),
        cards="\n".join(cards),
        csp=escape(_CSP),
    ).encode("utf-8")


def _read_utf8(path: Path, limit: int, context: str) -> str:
    try:
        return read_limited(path, limit).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WorkbenchError(f"{context} must use UTF-8 encoding.") from exc


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values = dict(pairs)
    if len(values) != len(pairs):
        raise WorkbenchError("The comparison manifest must not repeat JSON field names.")
    return values


def _object(value: object, fields: set[str], context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not value.keys() <= fields:
        allowed = ", ".join(sorted(fields))
        raise WorkbenchError(f"{context} must be an object with only these fields: {allowed}.")
    return value


def _text(
    entry: dict[str, object],
    key: str,
    limit: int,
    context: str,
    *,
    required: bool = False,
    trim: bool = True,
) -> str:
    if not required and key not in entry:
        return ""
    value = entry.get(key)
    if (
        not isinstance(value, str)
        or len(value) > limit
        or _INVALID_TEXT.search(value)
        or (required and not value.strip())
    ):
        requirement = "a nonblank string" if required else "a string"
        raise WorkbenchError(f"{context}: {key} must be {requirement} (up to {limit} characters).")
    return value.strip() if trim else value


def _card(index: int, name: str, summary: str, preview: str) -> str:
    description = (
        f'<p class="summary" dir="auto" id="summary-{index}">{summary}</p>' if summary else ""
    )
    described_by = f"preview-note summary-{index}" if summary else "preview-note"
    return f"""
      <li class="direction" id="direction-{index}" tabindex="-1">
        <header class="direction-heading">
          <p class="direction-number">Direction {index:02}</p>
          <h3 dir="auto">{name}</h3>
          {description}
        </header>
        <iframe sandbox="" referrerpolicy="no-referrer" tabindex="0"
          title="Direction {index:02} preview: {name}" aria-describedby="{described_by}"
          srcdoc="{preview}"></iframe>
      </li>"""


def _preview(source: str, context: str) -> str:
    parser = _StaticHTML()
    parser.feed(source)
    parser.close()
    body = "".join(parser.body).strip()
    if not body:
        raise WorkbenchError(f"{context} has no supported static HTML content.")
    return (
        f'<!doctype html><html{parser.html_attrs}><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{escape(_CSP)}">'
        '<meta name="referrer" content="no-referrer">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"{''.join(parser.styles)}</head><body{parser.body_attrs}>{body}</body></html>"
    )


class _StaticHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_attrs = ' lang="en"'
        self.body_attrs = ""
        self.body: list[str] = []
        self.styles: list[str] = []
        self._style: list[str] | None = None
        self._blocked: tuple[str, int] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._blocked:
            blocked, depth = self._blocked
            if tag == blocked:
                self._blocked = (blocked, depth + 1)
            return
        if tag in _DROP_CONTENT:
            self._blocked = (tag, 1)
        elif tag == "style":
            self._style = []
        elif tag == "html":
            self.html_attrs = self._attributes(tag, attrs) or ' lang="en"'
        elif tag == "body":
            self.body_attrs = self._attributes(tag, attrs)
        elif tag in _ELEMENTS:
            attributes = self._attributes(tag, attrs)
            if tag in _CONTROLS:
                attributes += " disabled"
            self.body.append(f"<{tag}{attributes}>")

    def handle_endtag(self, tag: str) -> None:
        if self._blocked:
            blocked, depth = self._blocked
            if tag == blocked:
                self._blocked = (blocked, depth - 1) if depth > 1 else None
            return
        if tag == "style" and self._style is not None:
            # CSS is raw text in HTML. Prevent a style closing sequence from being
            # interpreted as markup, even if the source parser treated it as CSS.
            css = re.sub(r"</style", r"\\3c /style", "".join(self._style), flags=re.I)
            self.styles.append(f"<style>{css}</style>")
            self._style = None
        elif tag in _ELEMENTS - _VOID:
            self.body.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._blocked:
            return
        if self._style is not None:
            self._style.append(data)
        else:
            self.body.append(escape(data, quote=False))

    @staticmethod
    def _attributes(tag: str, attrs: list[tuple[str, str | None]]) -> str:
        permitted = _ELEMENT_ATTRS.get(tag, ())
        safe: dict[str, str] = {}
        for key, value in attrs:
            value = value or ""
            if (
                key in _GLOBAL_ATTRS
                or key in permitted
                or (tag == "img" and key == "src" and _RASTER.fullmatch(value))
            ):
                safe.setdefault(key, value)
        if tag == "html":
            safe.setdefault("lang", "en")
        return "".join(f' {key}="{escape(value)}"' for key, value in safe.items())
