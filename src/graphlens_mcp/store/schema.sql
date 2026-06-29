CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  path       TEXT PRIMARY KEY,
  hash       TEXT NOT NULL,
  mtime      REAL NOT NULL,
  size       INTEGER NOT NULL,
  status     TEXT NOT NULL CHECK(status IN ('ok', 'degraded')),
  language   TEXT
);

CREATE TABLE IF NOT EXISTS nodes (
  id              TEXT PRIMARY KEY,
  kind            TEXT NOT NULL,
  qualified_name  TEXT NOT NULL,
  name            TEXT NOT NULL,
  file_path       TEXT,
  span_json       TEXT,
  metadata_json   TEXT
);

-- NOTE: edges intentionally have NO foreign key to nodes(id). A cross-file edge
-- (e.g. B -> A) references A by its stable id, and during an incremental re-index A
-- may be momentarily absent while its file is rebuilt. A FK with enforcement would
-- reject such "dangling" edges; instead we keep them and filter unresolved targets
-- at read time (joins drop rows whose node is missing). Referential integrity is
-- thus enforced by application code, by design (see ARCHITECTURE.md §"dangling edges").
CREATE TABLE IF NOT EXISTS edges (
  source_id     TEXT NOT NULL,
  target_id     TEXT NOT NULL,
  kind          TEXT NOT NULL,
  metadata_json TEXT,
  UNIQUE(source_id, target_id, kind)
);

CREATE TABLE IF NOT EXISTS deps (
  importer_path  TEXT NOT NULL,
  imported_path  TEXT NOT NULL,
  UNIQUE(importer_path, imported_path)
);

CREATE INDEX IF NOT EXISTS idx_edges_src  ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_tgt  ON edges(target_id);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);
CREATE INDEX IF NOT EXISTS idx_nodes_qname ON nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_deps_imported ON deps(imported_path);

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
  name,
  qualified_name,
  node_id UNINDEXED
);

-- Semantic clusters: groups of semantically-related symbols, recomputed from
-- node embeddings during a full index (see indexer/semantic.py). Like the rest
-- of the graph this is a regenerable cache — a schema change drops and rebuilds
-- it rather than migrating, and an incremental edit marks it stale (recomputed
-- lazily on the next cluster query). The semble retrieval index itself is NOT
-- stored here: it lives in a sidecar file written by semble's own save().
CREATE TABLE IF NOT EXISTS clusters (
  id     INTEGER PRIMARY KEY,
  label  TEXT NOT NULL,       -- short human label derived from member symbols
  size   INTEGER NOT NULL,    -- number of member nodes
  terms  TEXT                 -- JSON array of the cluster's top descriptive terms
);

-- A node belongs to at most one cluster (PRIMARY KEY on node_id). No foreign key
-- to nodes(id) for the same dangling-tolerance reason as edges: a cluster row may
-- briefly outlive a node mid-reindex; unresolved members are filtered at read time.
CREATE TABLE IF NOT EXISTS node_clusters (
  node_id    TEXT PRIMARY KEY,
  cluster_id INTEGER NOT NULL,
  score      REAL              -- similarity to the cluster centroid (higher = tighter)
);

CREATE INDEX IF NOT EXISTS idx_node_clusters_cluster ON node_clusters(cluster_id);
