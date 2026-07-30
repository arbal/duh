import os
import sqlite3

from conftest import EXPECT, MiB, Scanned, approx, node_id_for, run_duh


def freeable_of(scanned, path):
    """Exact (freeable, locked_here) via freeable_cache after a CLI run."""
    run_duh("freeable", path, db=scanned.db)
    con = sqlite3.connect(scanned.db)
    nid = node_id_for(con, path, scanned.root)
    row = con.execute(
        "SELECT freeable, locked_here FROM freeable_cache WHERE node_id = ?",
        (nid,)).fetchone()
    return (row[0], row[1]) if row else (0, 0)


def test_clone_dir_with_member_outside_frees_nothing(scanned):
    # family LCA is the root; deleting clones/ leaves big.bin holding the blocks
    f, _ = freeable_of(scanned, scanned.root / "clones")
    assert approx(f, 0)


def test_sibling_family_credits_lca(scanned):
    f_x, _ = freeable_of(scanned, scanned.root / "siblings/x")
    f_sib, lh_sib = freeable_of(scanned, scanned.root / "siblings")
    assert approx(f_x, 0)
    assert approx(f_sib, EXPECT["family_siblings"])
    assert approx(lh_sib, EXPECT["family_siblings"])


def test_hardlink_family_counted_once(scanned):
    f, _ = freeable_of(scanned, scanned.root / "hardlinks")
    assert approx(f, EXPECT["hardlinks"])


def test_unique_dir_fully_freeable(scanned):
    f, _ = freeable_of(scanned, scanned.root / "unique")
    assert approx(f, EXPECT["unique"])


def test_root_freeable_counts_each_family_once(scanned):
    f, _ = freeable_of(scanned, scanned.root)
    expected = (EXPECT["family_big"] + EXPECT["family_siblings"]
                + EXPECT["hardlinks"] + EXPECT["unique"]
                + EXPECT["sparse_alloc"] + (1 << 20))  # + excluded node_modules
    assert approx(f, expected, tol=1 << 20)


def test_freeable_cli_output_shape(scanned):
    out = run_duh("freeable", scanned.root / "unique", db=scanned.db).stdout
    assert "Freeable:" in out and "Locked here:" in out


def test_rescan_invalidates_freeable_cache(tmp_path):
    """Regression: the Python predecessor's `--rescan` reused the scans rowid,
    so month-old freeable_cache rows passed the scan_id validity check and
    `freeable` served stale numbers. Any completed scan must wipe the cache
    and the next `freeable` must reflect post-rescan reality."""
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "a.bin").write_bytes(os.urandom(2 * MiB))
    db = tmp_path / "scan.db"
    scanned = Scanned(db=db, root=root)

    run_duh("scan", root, "-q", db=db)
    f_before, _ = freeable_of(scanned, root)  # populates the cache
    assert approx(f_before, 2 * MiB)
    assert sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM freeable_cache").fetchone()[0] > 0

    (root / "sub/b.bin").write_bytes(os.urandom(3 * MiB))
    run_duh("scan", root, "--rescan", "-q", db=db)
    assert sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM freeable_cache").fetchone()[0] == 0  # cache wiped

    f_after, _ = freeable_of(scanned, root)  # fresh compute, not stale cache
    assert approx(f_after, 5 * MiB)
