from pydantic import BaseModel, ConfigDict

__all__ = ["CommitInfo"]


class CommitInfo(BaseModel):
    """The git commit an index run captured, for the temporal log.

    Produced by the feature layer from the checkout's git state and handed to
    the indexing pipeline, which stays git-agnostic. It intentionally carries
    no ordering number: the temporal log assigns a monotonic per-(project, ref)
    ``seq`` at append time keyed on ``sha`` (git commit depth is unusable —
    shallow clones make it collide and diverge between clones of one commit).
    """

    model_config = ConfigDict(frozen=True)

    ref: str
    """Branch/ref name the checkout is on (the commit sha if detached)."""
    sha: str
    """The HEAD commit sha — the stable per-commit identity across clones."""
    time_update: int
    """Committer epoch seconds of HEAD — the human/read time cursor."""
