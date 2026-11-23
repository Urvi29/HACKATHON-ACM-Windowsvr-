"""Disk panel: hardware listing and the per-filesystem usage scan."""
import os

from me3 import disks


def test_scan_mount_sums_by_kind_and_counts_hardlinks_once(tmp_path):
    (tmp_path / "Pictures").mkdir()
    (tmp_path / "Pictures" / "a.jpg").write_bytes(b"x" * 10000)
    (tmp_path / "notes.md").write_text("hello")
    (tmp_path / "blob").write_bytes(b"y" * 5000)
    os.link(tmp_path / "blob", tmp_path / "blob-link")
    (tmp_path / ".Trash-1000" / "files").mkdir(parents=True)
    (tmp_path / ".Trash-1000" / "files" / "old.txt").write_text("bye")
    try:  # symlinks need privileges on Windows (Developer Mode / admin); skip that part when unavailable
        os.symlink("/usr", tmp_path / "usr-link")
        symlink_ok = True
    except (OSError, NotImplementedError):
        symlink_ok = False

    r = disks.scan_mount(str(tmp_path))
    kinds = {k["kind"]: k for k in r["by_kind"]}
    assert r["files"] == (5 if symlink_ok else 4)  # hard link counted once, symlink skipped as a dir target
    assert kinds["image"]["n"] == 1 and kinds["image"]["bytes"] >= 10000
    assert kinds["other"]["n"] == (2 if symlink_ok else 1)  # blob (link counted once) + optional usr-link symlink
    assert kinds["document"]["n"] == 2  # notes.md + old.txt
    assert r["trash"] > 0
    assert any(f["path"].replace("\\", "/").endswith("/Pictures") for f in r["folders"])


def test_category_marks_os_and_app_data_as_system():
    assert disks._category(("usr", "lib", "x.so"), "x.so", "/") == "system"
    assert disks._category(("home", "me", ".cache", "a.jpg"), "a.jpg", "/") == "system"
    assert disks._category(("home", "me", "Pictures", "a.jpg"), "a.jpg", "/") == "image"
    assert disks._category(("vmlinuz",), "vmlinuz", "/boot") == "system"
    assert disks._category(("usr", "a.mp4"), "a.mp4", "/mnt/storage") == "video"  # only the root fs has OS folders


def test_hardware_lists_real_disks():
    ds = disks.hardware()
    assert ds, "lsblk should list at least one disk"
    for d in ds:
        assert d["size"] > 0 and d["model"]
        for p in d["partitions"]:
            assert p["used"] >= 0 and p["free"] >= 0
