import pytest


@pytest.mark.integration
@pytest.mark.registry
async def test_registry_roundtrips_projects(registry, project_factory):
    # Arrange — two distinct repositories, each a whole-repo project.
    first = project_factory.build(id="alpha_aaaa0000")
    second = project_factory.build(
        id="beta_bbbb1111",
        git_url="git@github.com:o/beta.git",
    )

    # Act
    await registry.add(first)
    await registry.add(second)
    listed = await registry.list_all()
    fetched = await registry.get("beta_bbbb1111")

    # Assert
    assert {p.id for p in listed} == {"alpha_aaaa0000", "beta_bbbb1111"}
    assert fetched is not None
    assert fetched.git_url == "git@github.com:o/beta.git"
