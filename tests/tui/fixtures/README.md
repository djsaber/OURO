# Welcome artwork reference frames

`welcome-frames.json.gz` records 240 frames evaluated with Python scalar math from the
built-in artwork samplers. Persistence, neighboring light, exposure, dot coverage and
background compositing are calculated independently of `PhosphorRaster` and its array
helpers. The fixture requires only the Python standard library to read.

Each case identifies a built-in style, terminal-cell dimensions, and color mode. Frames
follow the top-level `times` sequence, with an elapsed interval of `1 / 30` per call so
that the samples also cover phosphor persistence. Each Base64 payload contains row-major
cells of four bytes: Braille bit mask, red, green, blue. Mask zero represents an ordinary
space with no foreground, encoded with zero RGB; visible cells are composed over the
top-level `background` color. Sizes include a single cell,
a short viewport, and typical artwork areas. Time samples include initial, adjacent,
and long-running animation phases.

Regression checks require identical Braille masks and allow at most one 8-bit color
unit for platform math-library rounding. Changes to these fixtures require an intentional
artwork change and visual review; do not regenerate them merely to make a test pass.
