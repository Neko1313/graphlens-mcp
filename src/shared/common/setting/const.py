APP_NAME = "graphlens-mcp"

CODE_COLLECTION = "code_nodes"
"""The single shared vector collection.

Every project's embeddings live here, scoped by a ``project_id`` scalar on each
point — isolation is a filter, not a per-project collection. The point primary
key is the project-namespaced ``gid`` so ids never collide across projects.
"""
