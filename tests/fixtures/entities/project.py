from __future__ import annotations

import pytest
from polyfactory.factories.pydantic_factory import ModelFactory

from entities.project import Project


class ProjectFactory(ModelFactory[Project]):
    """Builds ``Project`` instances with only the fields a test cares about.

    Identity-shaped fields (``id``, ``subpath``, ``git_url``) are overridden
    per test; the rest are filled with arbitrary values so registry/resource
    tests stay focused on behavior, not on hand-built objects.
    """

    __model__ = Project


@pytest.fixture
def project_factory() -> type[ProjectFactory]:
    return ProjectFactory
