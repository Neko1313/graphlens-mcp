import pytest


@pytest.mark.integration
@pytest.mark.registry
async def test_registry_roundtrips_projects_including_subpath(
    registry, project_factory,
):
    # Arrange — a whole-repo project and a monorepo-subtree project.
    root_project = project_factory.build(id="mono_root_aaaa0000", subpath="")
    api_project = project_factory.build(
        id="mono_api_bbbb1111",
        subpath="services/api",
        git_url="git@github.com:o/mono.git",
    )

    # Act
    await registry.add(root_project)
    await registry.add(api_project)
    listed = await registry.list_all()
    fetched = await registry.get("mono_api_bbbb1111")

    # Assert
    assert {p.id for p in listed} == {"mono_root_aaaa0000", "mono_api_bbbb1111"}
    assert fetched is not None
    assert fetched.subpath == "services/api"
    assert fetched.git_url == "git@github.com:o/mono.git"
