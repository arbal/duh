use rusqlite::{Connection, OptionalExtension};
use std::path::{Path, PathBuf};

/// Schema version for the APFS-aware accounting cache.
pub const SCHEMA_VERSION: i64 = 4;

/// The scan schema, extended with explicit v4 accounting dimensions.
pub const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY,
  root TEXT NOT NULL,
  started_at REAL NOT NULL,
  finished_at REAL,
  files_count INTEGER,
  excluded_count INTEGER,
  bytes_logical INTEGER,
  bytes_blocks INTEGER,
  schema_version INTEGER DEFAULT 4
);

CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  parent_id INTEGER REFERENCES files(id),
  name TEXT NOT NULL,
  is_dir INTEGER NOT NULL,
  is_symlink INTEGER NOT NULL,
  is_excluded INTEGER NOT NULL DEFAULT 0,
  dev INTEGER NOT NULL,
  ino INTEGER NOT NULL,
  clone_id INTEGER,
  nlinks INTEGER NOT NULL,
  size_logical INTEGER NOT NULL,
  size_blocks INTEGER NOT NULL,
  excluded_file_count INTEGER,
  mtime INTEGER NOT NULL,
  private_size INTEGER,
  ext_flags INTEGER,
  clone_refcnt INTEGER,
  scan_id INTEGER NOT NULL REFERENCES scans(id),
  UNIQUE(parent_id, name)
);

CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent_id);
CREATE INDEX IF NOT EXISTS idx_files_clone ON files(clone_id) WHERE clone_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_files_inode ON files(dev, ino) WHERE nlinks > 1;
CREATE INDEX IF NOT EXISTS idx_files_excluded ON files(is_excluded) WHERE is_excluded = 1;

CREATE TABLE IF NOT EXISTS excluded_families (
  excluded_id INTEGER NOT NULL REFERENCES files(id),
  clone_id INTEGER NOT NULL,
  member_count INTEGER NOT NULL,
  blocks_sum INTEGER NOT NULL,
  max_blocks INTEGER NOT NULL,
  PRIMARY KEY (excluded_id, clone_id)
);
CREATE INDEX IF NOT EXISTS idx_excluded_families_clone ON excluded_families(clone_id);

CREATE TABLE IF NOT EXISTS freeable_cache (
  node_id INTEGER PRIMARY KEY,
  freeable INTEGER NOT NULL,
  locked_here INTEGER NOT NULL,
  guaranteed INTEGER NOT NULL DEFAULT 0,
  conditional_shared INTEGER NOT NULL DEFAULT 0,
  uncertain INTEGER NOT NULL DEFAULT 0,
  locked_guaranteed_here INTEGER NOT NULL DEFAULT 0,
  locked_conditional_here INTEGER NOT NULL DEFAULT 0,
  accounting_status TEXT NOT NULL DEFAULT 'v4',
  scan_id INTEGER NOT NULL
);
"#;

/// Resolve the default DB path: `DUH_DB` env var, or `~/.local/share/duh/scan.db`.
///
/// Note: this does not consider the `--db` CLI flag; callers should check that first.
pub fn default_db_path() -> PathBuf {
    if let Ok(p) = std::env::var("DUH_DB") {
        return PathBuf::from(p);
    }
    let home = std::env::var("HOME").expect("HOME not set");
    PathBuf::from(home).join(".local/share/duh/scan.db")
}

/// Open (creating if necessary) the database at `path`, enable WAL mode, foreign keys,
/// and apply the schema.
pub fn open(path: &Path) -> rusqlite::Result<Connection> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).ok();
    }
    let con = Connection::open(path)?;
    // Wait up to 5s on a locked DB rather than failing instantly with
    // SQLITE_BUSY (matches Python's sqlite3 default busy timeout).
    con.busy_timeout(std::time::Duration::from_secs(5))?;
    con.pragma_update(None, "journal_mode", "WAL")?;
    con.pragma_update(None, "synchronous", "NORMAL")?;
    con.pragma_update(None, "foreign_keys", "ON")?;
    con.execute_batch(SCHEMA)?;
    validate_schema(&con)?;
    Ok(con)
}

/// Reject an old database instead of allowing a stale v3 cache to masquerade
/// as v4 accounting. A database with no scans is valid and can be populated by
/// the current scanner.
fn validate_schema(con: &Connection) -> rusqlite::Result<()> {
    let latest: Option<i64> = con
        .query_row("SELECT schema_version FROM scans ORDER BY id DESC LIMIT 1", [], |r| r.get(0))
        .optional()?;
    if let Some(version) = latest {
        if version != SCHEMA_VERSION {
            return Err(schema_error(format!(
                "database schema v{version} is not supported; rescan/rebuild required for v{SCHEMA_VERSION}"
            )));
        }
    }
    let columns: std::collections::HashSet<String> = con
        .prepare("PRAGMA table_info(freeable_cache)")?
        .query_map([], |r| r.get::<_, String>(1))?
        .collect::<rusqlite::Result<_>>()?;
    for required in ["guaranteed", "conditional_shared", "uncertain", "locked_guaranteed_here", "locked_conditional_here", "accounting_status"] {
        if !columns.contains(required) {
            return Err(schema_error(format!(
                "database lacks v4 column {required}; rescan/rebuild required"
            )));
        }
    }
    Ok(())
}

fn schema_error(message: String) -> rusqlite::Error {
    rusqlite::Error::SqliteFailure(
        rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_SCHEMA),
        Some(message),
    )
}

#[cfg(test)]
mod tests {
    use rusqlite::Connection;

    #[test]
    fn schema_applies_and_tables_exist() {
        let tmp = std::env::temp_dir().join(format!("duh-test-{}.db", std::process::id()));
        let con = super::open(&tmp).unwrap();
        for t in ["scans", "files", "excluded_families", "freeable_cache"] {
            let n: i64 = con
                .query_row(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?1",
                    [t],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(n, 1, "missing table {t}");
        }
        let columns: std::collections::HashSet<String> = con
            .prepare("PRAGMA table_info(freeable_cache)")
            .unwrap()
            .query_map([], |r| r.get::<_, String>(1))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
        for required in [
            "guaranteed",
            "conditional_shared",
            "uncertain",
            "locked_guaranteed_here",
            "locked_conditional_here",
            "accounting_status",
        ] {
            assert!(columns.contains(required), "missing v4 column {required}");
        }
        std::fs::remove_file(&tmp).ok();
    }

    #[test]
    fn stale_schema_reports_an_actionable_schema_error() {
        let con = Connection::open_in_memory().unwrap();
        con.execute_batch(
            "CREATE TABLE scans (id INTEGER PRIMARY KEY, schema_version INTEGER); \
             CREATE TABLE freeable_cache (node_id INTEGER, freeable INTEGER, locked_here INTEGER); \
             INSERT INTO scans(id, schema_version) VALUES (1, 3);",
        )
        .unwrap();

        let err = super::validate_schema(&con).unwrap_err();
        assert!(
            err.to_string().contains("rescan/rebuild required"),
            "unexpected stale-schema error: {err:?}"
        );
    }
}
