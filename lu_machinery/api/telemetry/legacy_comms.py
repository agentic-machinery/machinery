"""Retired legacy comms route telemetry: the name stays, the writer is removed (#515).

The product's ``scripts/project_api/batch_progress.py`` still passes
``LegacyCommsTelemetryRoute`` as its router ``route_class``. It is now the plain
FastAPI route: nothing is recorded and no store is opened. Delete this module
once the product drops that import.
"""

from fastapi.routing import APIRoute

LegacyCommsTelemetryRoute = APIRoute
