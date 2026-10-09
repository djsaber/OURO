# Welcome artwork reference frames

`welcome-frames.json.gz` records rendered frames from commit `77c5f6e` using NumPy 2.5.3
on Windows x64 / Python 3.14.6. It is an independent visual baseline, not output generated
by the implementation under test. The fixture requires only the Python standard library.

Each case identifies a built-in style, terminal-cell dimensions, and color mode. Frames
follow the top-level `times` sequence, with an elapsed interval of `1 / 30` per call so
that the samples also cover phosphor persistence. Each Base64 payload contains row-major
cells of four bytes: Braille bit mask, red, green, blue. Sizes include a single cell,
a short viewport, and typical artwork areas. Time samples include initial, adjacent,
and long-running animation phases.

Regression checks require identical Braille masks and allow at most one 8-bit color
unit for platform math-library rounding. Changes to these fixtures require an intentional
artwork change and visual review; do not regenerate them merely to make a test pass.
