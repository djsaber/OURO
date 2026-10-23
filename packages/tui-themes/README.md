# OURO TUI themes

Install the complete optional welcome extension in the OURO environment:

```sh
pip install "ouro-agent[themes]"
```

This distribution contains both the shared `ouro_tui_themes.dot_matrix` engine and
the official `ouro_tui_themes.styles` collection. The engine owns numerical fields,
terminal rasterization and widget scheduling; styles own artwork and page composition.
The engine does not depend on style implementations or welcome-page contracts.

OURO's core provides the welcome API, persistent input surface, shared information
components and the `basic` fallback page. It runs without this distribution or NumPy.
The application loads style metadata from the installed compatible release and imports
renderers only when selected. Missing extensions or unavailable dependencies preserve
the saved selection while displaying `basic`; restarting after installation restores it.

Core and extension versions are independent. The core's `themes` extra pins a compatible extension
release; the extension declares its supported core range and NumPy requirement. Build both packages
from the OURO workspace with `uv build --all-packages`. Local installation checks use both wheels;
published installations resolve the extension version selected by the core package.
