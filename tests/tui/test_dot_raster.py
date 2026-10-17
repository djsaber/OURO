from __future__ import annotations

import pytest
from textual.app import App

from ouro.tui.welcome.dot_matrix.field import DotArtwork, buffer
from ouro.tui.welcome.dot_matrix.raster import PhosphorRaster
from ouro.tui.welcome.dot_matrix.widget import DotMatrix


def solid_artwork(red: float, green: float, blue: float) -> DotArtwork:
    def prepare(grid):
        light = tuple(buffer(len(grid.x), value) for value in (red, green, blue))
        return lambda seconds: light

    return DotArtwork("Solid light", prepare)


@pytest.mark.parametrize("monochrome", [False, True])
@pytest.mark.parametrize("light", [0.0, 0.0001])
def test_unlit_cells_are_spaces_without_foreground_or_background(light, monochrome):
    raster = PhosphorRaster(solid_artwork(light, light, light), 8, 4)
    for row in raster.frame(0, 0, monochrome=monochrome):
        assert row.text == " " * 8
        assert row.cell_length == 8
        assert len(list(row)) == 1
        for segment in row:
            assert segment.style.color is None
            assert segment.style.bgcolor is None


def test_dim_light_fades_by_extinguishing_fixed_dots():
    counts = []
    for light in (0.0, 0.001, 0.005, 0.02, 0.05, 0.15, 0.6, 2.0):
        raster = PhosphorRaster(solid_artwork(light, light, light), 3, 3)
        character = raster.frame(0, 0)[1].text[1]
        counts.append(0 if character == " " else (ord(character) - 0x2800).bit_count())
    assert counts[0] == counts[1] == 0
    assert 0 < counts[2] < counts[3] < 8
    assert counts == sorted(counts)
    assert counts[-1] == 8


def test_visible_light_keeps_its_hue_without_a_channel_floor():
    raster = PhosphorRaster(solid_artwork(0.2, 0.0, 0.0), 3, 3)
    for row in raster.frame(0, 0):
        for segment in row:
            assert segment.text.strip()
            red, green, blue = segment.style.color.get_truecolor()
            assert red > 0 and green == blue == 0


@pytest.mark.parametrize("background", [(12, 12, 12), (37, 32, 53), (240, 240, 240)])
def test_light_composites_over_canvas_without_darkening_it(background):
    raster = PhosphorRaster(solid_artwork(0.02, 0.0, 0.0), 3, 3)
    for row in raster.frame(0, 0, background=background):
        for segment in row:
            red, green, blue = segment.style.color.get_truecolor()
            assert red > background[0]
            assert green == background[1] and blue == background[2]
            assert segment.style.bgcolor is None


def test_extinguished_cells_clear_once_and_ignore_invisible_light_changes():
    def prepare(grid):
        return lambda seconds: tuple(buffer(len(grid.x), seconds) for _ in range(3))

    raster = PhosphorRaster(DotArtwork("Fading light", prepare), 8, 4)
    assert all(row.text.strip() for row in raster.frame(1, 0))
    dark = raster.frame(0, 2)
    assert all(row.text == " " * 8 for row in dark)
    assert sum(region.area for region in raster.damage) == 32
    same = raster.frame(0.0001, 2)
    assert all(before is after for before, after in zip(dark, same, strict=True))
    assert not raster.damage


@pytest.mark.parametrize("monochrome", [False, True])
def test_light_persists_between_frames_and_decays_to_an_empty_canvas(monochrome):
    def prepare(grid):
        lit = tuple(buffer(len(grid.x), 0.2) for _ in range(3))
        dark = tuple(buffer(len(grid.x)) for _ in range(3))
        return lambda seconds: lit if seconds == 0 else dark

    raster = PhosphorRaster(DotArtwork("One flash", prepare), 3, 3)
    lit = raster.frame(0, 0, monochrome=monochrome)
    assert all(row.text.strip() for row in lit)
    assert raster.frame(1, 0, monochrome=monochrome) == lit
    fading = raster.frame(1, 0.05, monochrome=monochrome)
    assert any(row.text.strip() for row in fading)
    assert max(segment.style.color.get_truecolor().red for row in fading for segment in row) < max(
        segment.style.color.get_truecolor().red for row in lit for segment in row
    )
    assert all(row.text == " " * 3 for row in raster.frame(1, 2, monochrome=monochrome))


@pytest.mark.parametrize("monochrome", [False, True])
async def test_dark_matrix_inherits_canvas_when_background_changes(monkeypatch, monochrome):
    class ArtworkApp(App):
        CSS = "DotMatrix {width: 8; height: 4;}"

        def compose(self):
            yield DotMatrix(solid_artwork(0, 0, 0), motion=False)

    if monochrome:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)
    app = ArtworkApp()
    async with app.run_test(size=(12, 6)) as pilot:
        matrix = app.query_one(DotMatrix)
        await app.workers.wait_for_complete()
        assert matrix._lines
        for background in ("#000000", "#0c0c0c", "#252035", "#f0f0f0"):
            app.screen.styles.background = background
            await pilot.pause()
            for row in app.screen._compositor.render_strips()[:4]:
                assert row.text == " " * 12
                segments = list(row)
                canvas = segments[-1].style.bgcolor
                if not monochrome:
                    assert canvas.name == background
                assert all(segment.style.bgcolor == canvas for segment in segments)


async def test_paused_matrix_recolors_when_canvas_theme_changes(monkeypatch):
    class ArtworkApp(App):
        CSS = "DotMatrix {width: 8; height: 4;}"

        def compose(self):
            yield DotMatrix(solid_artwork(0.2, 0, 0), motion=False)

    monkeypatch.delenv("NO_COLOR", raising=False)
    app = ArtworkApp()
    async with app.run_test(size=(12, 6)) as pilot:
        matrix = app.query_one(DotMatrix)
        await app.workers.wait_for_complete()
        for background in ("#0c0c0c", "#252035"):
            app.screen.styles.background = background
            matrix.refresh_theme()
            await app.workers.wait_for_complete()
            await pilot.pause()
            for segment in matrix.render_line(0):
                red, green, blue = segment.style.color.get_truecolor()
                canvas = segment.style.bgcolor.get_truecolor()
                assert red > canvas.red and green == canvas.green and blue == canvas.blue
            assert not matrix.motion and matrix.elapsed == 0
