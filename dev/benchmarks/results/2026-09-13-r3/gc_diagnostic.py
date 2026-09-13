"""Control preparation garbage before measuring the unchanged pipeline probe.

Usage: python gc_diagnostic.py SOURCE --mode tui --store-delay-ms 5
This excludes an explicit collection before the probe from timing. GC remains
enabled during the whole run. This is a diagnostic, not cold-start evidence.
"""

import asyncio
import gc
import json
import runpy
import sys
from pathlib import Path

source = Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(source))
namespace = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "session_pipeline.py"), run_name="pipeline_probe"
)
probe = namespace["Probe"]
initialize = probe.__init__


def initialize_after_collection(self, *args, **kwargs):
    gc.collect()
    initialize(self, *args, **kwargs)


probe.__init__ = initialize_after_collection
print(json.dumps({"diagnostic": "collect before probe; GC enabled during measurement"}), flush=True)
asyncio.run(namespace["main"]())
