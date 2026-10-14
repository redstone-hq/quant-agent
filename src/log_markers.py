"""Log-line markers shared by the code that writes a line and the code that reads it.

A leaf module: no `src` imports, so a reader (the health report, reachable
from the API) can import a marker without gaining an import route to the
writer and the money path behind it.
"""

from __future__ import annotations

#: The log prefix a reader greps for. One name for both coverage-sweep entry points.
SWEEP_LOG_NAME = "COVERAGE SWEEP"
