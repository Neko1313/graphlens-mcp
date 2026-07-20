from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.common.setting.type import MilvusDsn, Neo4jDsn


class DBSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DB__",
    )
    graph: Neo4jDsn | None = Field(
        None,
    )
    vector: MilvusDsn | None = Field(
        None,
    )
