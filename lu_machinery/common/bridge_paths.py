"""Legacy broker database path, kept only for the product-facing MonitorContext roots.

Machinery no longer reads this store (#515). The product's project API still reads
``ctx.stores.message_db`` until it drops its module comms view; then remove this module.
"""

import os
from pathlib import Path


def default_bridge_db_path(primary_repo_root: Path) -> Path:
    """Return the bridge DB owned by the primary checkout."""
    return primary_repo_root / ".mcp" / "servers" / "message-broker" / "messages.db"


def configured_bridge_db_path(primary_repo_root: Path) -> Path:
    """Apply the bridge's optional database override to its default path."""
    return Path(os.environ.get("AB_DB_PATH", str(default_bridge_db_path(primary_repo_root))))
