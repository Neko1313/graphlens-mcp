from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, UrlConstraints
from pydantic.networks import AnyUrl
from pydantic_core import MultiHostUrl


class Neo4jDsn(MultiHostUrl):
    _constraints = UrlConstraints(
        host_required=True,
        default_port=7687,
        allowed_schemes=[
            "neo4j",
            "neo4j+s",
            "neo4j+ssc",
            "bolt",
            "bolt+s",
            "bolt+ssc",
        ],
    )

    @property
    def host(self) -> str | None:
        return (self.hosts() or [{}])[0].get("host")

class MilvusDsn(AnyUrl):
    _constraints = UrlConstraints(
        host_required=True,
        default_port=19530,
        allowed_schemes=["http", "https"],
    )


class DBType(StrEnum):
    LOCAL = "LOCAL"
    HOST = "HOST"


class GraphDBLocal(BaseModel):
    type: Literal[DBType.LOCAL] = DBType.LOCAL
    path: Path


class GraphDBHost(BaseModel):
    type: Literal[DBType.HOST] = DBType.HOST
    dsn: Neo4jDsn


class VectorDBLocal(BaseModel):
    type: Literal[DBType.LOCAL] = DBType.LOCAL
    path: Path


class VectorDBHost(BaseModel):
    type: Literal[DBType.HOST] = DBType.HOST
    dsn: MilvusDsn

VectorDB = Annotated[
    VectorDBLocal | VectorDBHost,
    Field(discriminator="type"),
]

GraphDB = Annotated[
    GraphDBLocal | GraphDBHost,
    Field(discriminator="type"),
]
