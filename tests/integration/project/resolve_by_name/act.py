import pytest

from entities.project import Project
from shared.common.db.registry import resolve_project


async def _add(registry, project_id: str, name: str) -> None:
    await registry.add(
        Project(
            id=project_id,
            name=name,
            path=f"/tmp/{name}",
            git_url=f"https://example.invalid/{name}",
        ),
    )


@pytest.mark.integration
@pytest.mark.project
async def test_a_project_resolves_by_name_and_by_id_prefix(registry):
    # An id carries a hash of the git remote, which no caller can guess. Making
    # the name work is what stops a tool call being spent on list_projects.
    await registry.ensure_schema()
    await _add(registry, "hono_fe98d8970535", "hono")
    await _add(registry, "gin_13e11a3478b3", "gin")

    assert await resolve_project(registry, "hono") == "hono_fe98d8970535"
    assert await resolve_project(registry, "HONO") == "hono_fe98d8970535"
    assert await resolve_project(registry, "hono_fe98") == "hono_fe98d8970535"
    assert await resolve_project(registry, "gin_13e11a3478b3") == (
        "gin_13e11a3478b3"
    )


@pytest.mark.integration
@pytest.mark.project
async def test_the_only_project_answers_whatever_name_was_passed(registry):
    # Models fill `project` from whatever name is in front of them — the crate
    # in a qualified_name, the package, the directory. With one project indexed
    # there is nothing else it could mean, so answer instead of rejecting.
    await registry.ensure_schema()
    await _add(registry, "ripgrep_58e5e271826e", "ripgrep")

    assert await resolve_project(registry, "grep-printer") == (
        "ripgrep_58e5e271826e"
    )


@pytest.mark.integration
@pytest.mark.project
async def test_an_unmatched_project_names_the_ones_that_exist(registry):
    # The error is the caller's only listing, so it has to carry the answer.
    await registry.ensure_schema()
    await _add(registry, "hono_fe98d8970535", "hono")
    await _add(registry, "gin_13e11a3478b3", "gin")

    with pytest.raises(ValueError, match="unknown project: nope") as err:
        await resolve_project(registry, "nope")
    assert "hono (hono_fe98d8970535)" in str(err.value)
