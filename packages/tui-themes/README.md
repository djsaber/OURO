# OURO TUI themes

Install the complete optional welcome extension in the OURO environment:

```sh
pip install "ouro-agent[themes]"
```

## Organization

```text
ouro_tui_themes/
  tools/                  Theme-neutral numerical and terminal tools
    dot_matrix/           Grid, sampler contract, phosphor, raster and worker
    rendering.py          Frame-owned color runs and Textual adaptation
  packs/
    particle_art/         Particle Art: Horizon and Quantum veil
    matrix/               The Matrix: Matrix glyph rain
```

Each pack exports `PACKAGE: ThemePackage` from `ouro.tui.welcome`, with `package_id`, `name`, `description`,
`author` and a tuple of `themes`. The bundled catalog is `ouro_tui_themes.packs.PACKAGES`.
Packs are collections inside this distribution, not separately installed plugins.

| Package | Theme | Stable theme ID |
|---|---|---|
| Particle Art (`particle-art`) | Horizon | `event-horizon` |
| Particle Art (`particle-art`) | Quantum veil | `quantum-veil` |
| The Matrix (`matrix`) | Matrix | `matrix` |

The host loads `packs.PACKAGES` directly. The Theme menu lists the packages, then
opens a package's themes with its description and author. Esc restores the parent
filter and selection. Themes use `WelcomeStyle` and keep stable saved IDs.
Catalog imports load metadata only; each page factory imports its renderer when
selected. Matrix needs no NumPy.

To add a theme, define its `WelcomeStyle` and lazy page factory in the owning pack.
To add a pack, export its `PACKAGE` and include it in `packs.PACKAGES`. Pack-specific
layout, artwork and interaction stay in that pack; packs do not import one another.
Only reusable, theme-neutral capabilities belong in `tools`, which imports neither
the packs nor OURO. The numerical field module also remains independent of UI libraries.

## Particle composition and performance

Horizon and Quantum veil share `tools.dot_matrix` and require `numpy>=2.3.3,<3`.
The engine owns grids, phosphor/exposure, Braille/RGB encoding and one serial worker.
Themes supply size-prepared samplers through `DotArtwork.prepare` and select the
numerical backend through `DotArtwork.engine_factory`.

The particle canvas occupies the area above the bottom input, with one empty row between
them. Growing the input or opening its menu covers the field without rebuilding
its geometry or numerical buffers. The workspace path shares the native status row and
hides on narrow terminals. The version remains at the startup screen's bottom-right
position when it fits beside the input; otherwise it hides without reserving space.

The visual reference is [PHOSPHOR](https://01a0fe5a-80d4-7266-ab74-3a566afa781b.arena.site/).
Horizon uses horizontal pointer position to tilt its disk. Quantum veil varies
source separation, coherence and wave phase over time; vertical pointer position
adjusts its interference frequency. Pointer response uses time-based easing,
returning to neutral on leave. Neither theme has a click effect.

`packs/particle_art/particle_page.py` shares the layout; input, focus and menus remain owned
by the host. Pointer events replace one scalar target. Samplers cache geometry,
edge fades and bounded noise state per size, reusing work buffers each frame.
Quantum uses float32 scratch arrays with float64 time phases and output radiance;
both themes retain full terminal RGB precision.

## Host compatibility

OURO owns the welcome API, persistent input and the `basic` fallback page. It runs
without this distribution or NumPy. Missing extensions or unavailable dependencies
preserve the saved selection while displaying `basic`; restarting after installation
restores it.

Core and extension versions are independent. The core's `themes` extra declares
a minimum compatible extension release; the extension declares its supported core
range and NumPy requirement. Build both packages with `uv build --all-packages`.
Local installation checks use both wheels; published installations resolve the
extension version selected by the core package.
