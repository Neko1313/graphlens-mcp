from functools import cache
from importlib.metadata import metadata
from pathlib import Path

from platformdirs import PlatformDirs
from platformdirs.macos import MacOS
from platformdirs.unix import Unix
from platformdirs.windows import Windows

from shared.common.setting.const import APP_NAME
from shared.common.setting.env import DBSettings
from shared.common.setting.type import (
    GraphDB,
    GraphDBHost,
    GraphDBLocal,
    VectorDB,
    VectorDBHost,
    VectorDBLocal,
)
from shared.common.setting.util import create_dir


@cache
def get_setting_db() -> DBSettings:
    return DBSettings()

@cache
def get_app_dir() -> Unix | MacOS | Windows:
    return PlatformDirs(
        APP_NAME,
        metadata("graphlens-mcp")["Author"]
    )

@cache
def get_app_dir_data() -> Path:
    return create_dir(get_app_dir().user_data_dir)

@cache
def get_graph_db() -> GraphDB:
    db_settings = get_setting_db()

    if db_settings.graph is None:
        return GraphDBLocal(
            path=get_app_dir_data().joinpath("graph.db"),
        )

    return GraphDBHost(
        dsn=db_settings.graph,
    )

@cache
def get_vector_db() -> VectorDB:
    db_settings = get_setting_db()

    if db_settings.vector is None:
        return VectorDBLocal(
            path=get_app_dir_data().joinpath("vector.db"),
        )

    return VectorDBHost(
        dsn=db_settings.vector,
    )
