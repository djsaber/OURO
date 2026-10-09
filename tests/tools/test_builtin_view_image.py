import asyncio
import base64
import threading
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from ouro.core.cancellation import AbortSignal
from ouro.core.content import ImageContent
from ouro.tools import (
    ToolExecutionContext,
    ToolExecutor,
    ToolInvocation,
    ToolRegistry,
    builtin_tools,
)
from ouro.tools import builtin_view_image as view_image
from ouro.tools.schema import tool_argument_error


def image_file(path, *, size=(80, 40), format="PNG", mode="RGBA", **options):
    with Image.new(mode, size, (30, 90, 150, 100) if mode == "RGBA" else (30, 90, 150)) as image:
        image.save(path, format=format, **options)
    return path


def image_bytes(result):
    block = next(part for part in result.content_blocks if isinstance(part, ImageContent))
    return base64.b64decode(block.uri.partition(",")[2], validate=True)


async def execute(path, *, context=None, **arguments):
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    outcome = await ToolExecutor(registry).execute(
        ToolInvocation(
            session_id="s",
            run_id="r",
            tool_name="ViewImage",
            arguments={"path": str(path), **arguments},
        ),
        context=context,
    )
    assert outcome.result is not None
    return outcome.result


def test_view_image_is_registered_with_codex_argument_contract_and_read_only_effect():
    tool = next(tool for tool in builtin_tools() if tool.definition.name == "ViewImage")
    definition = tool.definition
    schema = definition.parameters_schema
    assert schema["required"] == ["path"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"path", "detail"}
    assert schema["properties"]["detail"]["enum"] == ["high", "original"]
    assert schema["properties"]["detail"]["default"] == "high"
    assert definition.effect == "read_only" and definition.permission_mode == "auto"


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"path": ""},
        {"path": 3},
        {"path": "x", "detail": None},
        {"path": "x", "detail": "low"},
        {"path": "x", "url": "https://example.test"},
    ],
)
async def test_invalid_arguments_fail_in_schema_and_direct_execution(arguments):
    tool = view_image.builtin_view_image_tool()
    assert tool_argument_error(tool.definition, arguments)
    with pytest.raises(ValueError):
        await tool.execute(
            ToolInvocation(session_id="s", run_id="r", tool_name="ViewImage", arguments=arguments),
            context=ToolExecutionContext(),
        )


@pytest.mark.parametrize("format,mode", [("PNG", "RGBA"), ("JPEG", "RGB"), ("WEBP", "RGBA")])
@pytest.mark.parametrize("detail", ["high", "original"])
async def test_native_static_images_keep_their_validated_bytes_without_upsampling(
    tmp_path, format, mode, detail
):
    path = image_file(tmp_path / "misleading.extension", format=format, mode=mode)
    raw = path.read_bytes()
    result = await execute(path, detail=detail)
    assert not result.is_error
    assert image_bytes(result) == raw
    assert result.structured["width"] == 80 and result.structured["height"] == 40
    assert result.structured["resized"] is False
    assert result.structured["detail"] == detail
    assert base64.b64encode(raw).decode() not in result.text_content
    assert "data:" not in str(result.structured)


@pytest.mark.parametrize("size", [(4096, 1024), (1024, 4096)])
@pytest.mark.parametrize("format,mode", [("PNG", "RGBA"), ("JPEG", "RGB")])
async def test_high_resizes_with_aspect_ratio_and_transparency_while_original_keeps_resolution(
    tmp_path, size, format, mode
):
    path = image_file(tmp_path / "screen", size=size, format=format, mode=mode)
    raw = path.read_bytes()
    high = await execute(path)
    original = await execute(path, detail="original")
    assert not high.is_error and not original.is_error
    with Image.open(BytesIO(image_bytes(high))) as image:
        assert image.size == (size[0] // 2, size[1] // 2)
        assert image.getpixel((0, 0))[3] == (100 if mode == "RGBA" else 255)
    assert high.structured["resized"] is True
    assert image_bytes(original) == raw
    assert original.structured["width"] == size[0]
    assert original.structured["height"] == size[1]
    assert path.read_bytes() == raw


async def test_original_applies_exif_orientation_without_resampling(tmp_path):
    exif = Image.Exif()
    exif[274] = 6
    path = image_file(tmp_path / "rotated.jpg", mode="RGB", format="JPEG", exif=exif)
    result = await execute(path, detail="original")
    assert not result.is_error
    with Image.open(BytesIO(image_bytes(result))) as image:
        assert image.size == (40, 80)
        assert image.getexif().get(274, 1) == 1
    assert result.structured["resized"] is False


@pytest.mark.parametrize("format", ["BMP", "TIFF"])
async def test_other_supported_raster_formats_are_normalized_to_png(tmp_path, format):
    path = image_file(tmp_path / "input", format=format)
    result = await execute(path)
    assert not result.is_error and result.structured["media_type"] == "image/png"
    with Image.open(BytesIO(image_bytes(result))) as image:
        assert image.size == (80, 40)


@pytest.mark.parametrize("format", ["GIF", "WEBP", "PNG"])
async def test_animated_images_explicitly_return_only_the_first_frame(tmp_path, format):
    path = tmp_path / "animated"
    with Image.new("RGB", (16, 8), "red") as first, Image.new("RGB", (16, 8), "blue") as second:
        first.save(
            path,
            format=format,
            save_all=True,
            append_images=[second],
            duration=100,
            loop=0,
            lossless=True,
        )
    result = await execute(path, detail="original")
    assert not result.is_error and result.structured["first_frame"] is True
    assert "first frame" in result.text_content
    with Image.open(BytesIO(image_bytes(result))) as image:
        assert image.size == (16, 8)
        assert image.getpixel((0, 0)) == (255, 0, 0, 255)
        assert not getattr(image, "is_animated", False)


@pytest.mark.parametrize("kind", ["missing", "directory", "text", "truncated", "svg"])
async def test_invalid_files_return_actionable_text_errors_without_pixels(tmp_path, kind):
    path = tmp_path / "input.png"
    if kind == "directory":
        path.mkdir()
    elif kind == "truncated":
        image_file(path)
        path.write_bytes(path.read_bytes()[:50])
    elif kind != "missing":
        path.write_text("<svg></svg>" if kind == "svg" else "not an image")
    result = await execute(path)
    assert result.is_error and result.text_content
    assert not any(isinstance(part, ImageContent) for part in result.content_blocks)


async def test_path_boundaries_include_artifacts_and_readable_roots_but_reject_escape(tmp_path):
    workspace, artifacts, readable = (
        tmp_path / name for name in ("workspace", "artifacts", "readable")
    )
    for directory in (workspace, artifacts, readable):
        directory.mkdir()
        image_file(directory / "image.png")
    outside = image_file(tmp_path / "outside.png")
    context = ToolExecutionContext(
        workspace=workspace,
        artifact_root=artifacts,
        readable_roots=(readable,),
        workspace_only=True,
    )
    for path in ("image.png", artifacts / "image.png", readable / "image.png"):
        assert not (await execute(path, context=context)).is_error
    for path in (outside, "../outside.png"):
        result = await execute(path, context=context)
        assert result.is_error and result.structured["error_type"] == "ToolPolicyError"
    link = workspace / "link.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    assert (await execute(link, context=context)).structured["error_type"] == "ToolPolicyError"


async def test_input_and_pixel_limits_fail_before_reading_or_decoding(tmp_path, monkeypatch):
    path = image_file(tmp_path / "input.png")
    monkeypatch.setattr(view_image, "_MAX_INPUT_BYTES", 1)
    result = await execute(path)
    assert result.is_error and "input limit" in result.text_content
    monkeypatch.setattr(view_image, "_MAX_INPUT_BYTES", 1024)
    monkeypatch.setattr(view_image, "_MAX_PIXELS", 1)
    monkeypatch.setattr(Image.Image, "load", lambda _: pytest.fail("pixels must not be decoded"))
    result = await execute(path)
    assert result.is_error and "pixel inspection limit" in result.text_content


@pytest.mark.parametrize("format", ["PNG", "BMP"])
async def test_media_limit_returns_text_instead_of_a_truncated_data_url(tmp_path, format):
    path = image_file(tmp_path / "input", format=format)
    result = await execute(path, context=ToolExecutionContext(max_output_media_chars=32))
    assert result.is_error and "media output limit" in result.text_content
    assert "data:" not in result.text_content


@pytest.mark.parametrize("abort", [False, True])
async def test_image_processing_keeps_event_loop_responsive_and_honors_cancellation(
    tmp_path, monkeypatch, abort
):
    path = image_file(tmp_path / "input.png")
    entered, release = threading.Event(), threading.Event()
    original_read = view_image._read_image

    def slow_read(path):
        entered.set()
        release.wait(5)
        return original_read(path)

    monkeypatch.setattr(view_image, "_read_image", slow_read)
    signal = AbortSignal()
    task = asyncio.create_task(execute(path, context=ToolExecutionContext(signal=signal)))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not task.done()
        if abort:
            signal.abort("cancel image inspection")
    finally:
        release.set()
    result = await task
    assert result.is_error == abort
    if abort:
        assert result.structured["aborted"]
        assert not any(isinstance(part, ImageContent) for part in result.content_blocks)


async def test_file_changed_during_read_is_rejected(tmp_path, monkeypatch):
    path = image_file(tmp_path / "input.png")
    original_stat = view_image.os.fstat
    calls = 0

    def changed_stat(fd):
        nonlocal calls
        observed = original_stat(fd)
        calls += 1
        return SimpleNamespace(
            st_mode=observed.st_mode,
            st_size=observed.st_size,
            st_mtime_ns=observed.st_mtime_ns + (calls - 1),
        )

    monkeypatch.setattr(view_image.os, "fstat", changed_stat)
    result = await execute(path)
    assert result.is_error and "changed while being read" in result.text_content
