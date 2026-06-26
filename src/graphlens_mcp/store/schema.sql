CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  path       TEXT PRIMARY KEY,
  hash       TEXT NOT NULL,
  mtime      REAL NOT NULL,
  size       INTEGER NOT NULL,
  status     TEXT NOT NULL CHECK(status IN ('skeleton', 'ok', 'degraded')),
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
CREATE INDEX IF NOT EXISTS idx_deps_imported ON deps(imported_path);

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
  name,
  qualified_name,
  node_id UNINDEXED
);
