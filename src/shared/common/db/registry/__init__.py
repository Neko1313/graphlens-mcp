from functools import cache

from shared.common.db.registry.kuzu import KuzuProjectRegistry
from shared.common.db.registry.port import ProjectRegistry
from shared.common.setting.getter_setting import get_registry_db_path

__all__ = [
    "ProjectRegistry",
    "get_registry_store",
]


@cache
def get_registry_store() -> ProjectRegistry:
    """Open the global project registry.

    Always local metadata (an embedded Kuzu ``registry.db``) — independent of
    which backend serves the per-project code graph.
    """
    return KuzuProjectRegistry.open(get_registry_db_path())
