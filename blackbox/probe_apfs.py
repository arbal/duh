import os
import sys
import json
import time
import shutil
import platform
import tempfile
import subprocess
import argparse

def get_free_bytes(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize

def wait_for_stable_free_space(path, timeout=30):
    start_time = time.time()
    last_free = get_free_bytes(path)
    stable_count = 0

    while True:
        time.sleep(1)
        current_free = get_free_bytes(path)
        if current_free == last_free:
            stable_count += 1
            if stable_count >= 3:
                return current_free
        else:
            stable_count = 0
            last_free = current_free

        if time.time() - start_time > timeout:
            print("WARNING: Free space did not stabilize within timeout.")
            return current_free

def run_cmd(cmd, **kwargs):
    print(f"+ {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kwargs)

def parse_metadata(output):
    meta = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()
        meta[key] = val
    return meta

def duh_scan(db_path, scan_path, rescan=False):
    cmd = ["cargo", "run", "--", "--db", db_path, "scan", scan_path, "--min-free", "0"]
    if rescan:
        cmd.append("--rescan")
    run_cmd(cmd)

def duh_file(db_path, file_path):
    cmd = ["cargo", "run", "--", "--db", db_path, "file", file_path]
    res = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return parse_metadata(res.stdout)

def main():
    if platform.system() != "Darwin":
        print("This probe is designed for macOS (Darwin) only.")
        sys.exit(1)

    print("--- Environment Info ---")
    run_cmd(["sw_vers"])
    run_cmd(["uname", "-a"])
    subprocess.run(["cargo", "run", "--", "--version"], check=True)

    with tempfile.TemporaryDirectory() as td:
        img_path = os.path.join(td, "apfs_test.sparseimage")
        mnt_path = os.path.join(td, "mnt")
        os.makedirs(mnt_path)
        db_path = os.path.join(td, "scan.db") # OUTSIDE the mounted image

        # Create a 512 MiB disposable APFS sparse image
        run_cmd(["hdiutil", "create", "-size", "512m", "-type", "SPARSE", "-fs", "APFS", "-volname", "duh_apfs_probe", img_path])

        # Mount the image
        res = subprocess.run(["hdiutil", "attach", img_path, "-mountpoint", mnt_path, "-nobrowse"], check=True, capture_output=True, text=True)
        print(res.stdout)

        dev_node = None
        for line in res.stdout.splitlines():
            if mnt_path in line:
                dev_node = line.split()[0]
                break

        if not dev_node:
            # Fallback if the path didn't match exactly
            dev_node = res.stdout.splitlines()[0].split()[0]

        print(f"Mounted at {mnt_path} on device {dev_node}")

        try:
            # === Case 1: clone_refcnt convention ===
            print("\n--- Case 1: clone_refcnt convention ---")
            c1_dir = os.path.join(mnt_path, "case1")
            os.makedirs(c1_dir)
            ref_1 = os.path.join(c1_dir, "ref_1")

            with open(ref_1, "wb") as f:
                f.write(os.urandom(1024 * 1024))

            duh_scan(db_path, c1_dir)
            m1 = duh_file(db_path, ref_1)
            print(f"File 1 (alone): clone_refcnt = {m1.get('clone_refcnt')}")

            ref_2 = os.path.join(c1_dir, "ref_2")
            run_cmd(["cp", "-c", ref_1, ref_2])

            duh_scan(db_path, c1_dir, rescan=True)
            m1_2 = duh_file(db_path, ref_1)
            m2_2 = duh_file(db_path, ref_2)
            print(f"File 1 (with 1 clone): clone_refcnt = {m1_2.get('clone_refcnt')}")
            print(f"File 2 (the clone)   : clone_refcnt = {m2_2.get('clone_refcnt')}")

            ref_3 = os.path.join(c1_dir, "ref_3")
            run_cmd(["cp", "-c", ref_1, ref_3])

            duh_scan(db_path, c1_dir, rescan=True)
            m1_3 = duh_file(db_path, ref_1)
            m3_3 = duh_file(db_path, ref_3)
            print(f"File 1 (with 2 clones): clone_refcnt = {m1_3.get('clone_refcnt')}")
            print(f"File 3 (the clone 2)  : clone_refcnt = {m3_3.get('clone_refcnt')}")

            # === Case 2: hardlink + private_size ===
            print("\n--- Case 2: hardlink + private_size ---")
            c2_dir = os.path.join(mnt_path, "case2")
            os.makedirs(c2_dir)
            hl_1 = os.path.join(c2_dir, "hl_1")

            with open(hl_1, "wb") as f:
                f.write(os.urandom(8 * 1024 * 1024))

            hl_2 = os.path.join(c2_dir, "hl_2")
            os.link(hl_1, hl_2)

            duh_scan(db_path, c2_dir)
            h1_m = duh_file(db_path, hl_1)
            h2_m = duh_file(db_path, hl_2)
            print(f"hl_1: private_size = {h1_m.get('private_size')}, nlinks = {h1_m.get('nlinks')}")
            print(f"hl_2: private_size = {h2_m.get('private_size')}, nlinks = {h2_m.get('nlinks')}")

            free_before_del1 = wait_for_stable_free_space(mnt_path)
            os.unlink(hl_1)
            free_after_del1 = wait_for_stable_free_space(mnt_path)
            print(f"Freed by deleting hl_1: {free_after_del1 - free_before_del1} bytes")

            os.unlink(hl_2)
            free_after_del2 = wait_for_stable_free_space(mnt_path)
            print(f"Freed by deleting hl_2: {free_after_del2 - free_after_del1} bytes")

            # === Case 3: partially-diverged clones ===
            print("\n--- Case 3: partially-diverged clones ---")

            # Subcase A
            print("\n  Subcase A: Delete MUTATED first")
            c3a_dir = os.path.join(mnt_path, "case3a")
            os.makedirs(c3a_dir)
            orig_a = os.path.join(c3a_dir, "orig")
            clone_a = os.path.join(c3a_dir, "clone")

            with open(orig_a, "wb") as f:
                f.write(os.urandom(2 * 1024 * 1024))

            run_cmd(["cp", "-c", orig_a, clone_a])

            with open(clone_a, "r+b") as f:
                f.seek(1024 * 1024)
                f.write(os.urandom(512 * 1024))

            duh_scan(db_path, c3a_dir)
            oa_m = duh_file(db_path, orig_a)
            ca_m = duh_file(db_path, clone_a)
            print(f"Orig: size_blocks={oa_m.get('size_blocks')}, private_size={oa_m.get('private_size')}, ext_flags={oa_m.get('ext_flags')}, clone_id={oa_m.get('clone_id')}, clone_refcnt={oa_m.get('clone_refcnt')}")
            print(f"Mutated: size_blocks={ca_m.get('size_blocks')}, private_size={ca_m.get('private_size')}, ext_flags={ca_m.get('ext_flags')}, clone_id={ca_m.get('clone_id')}, clone_refcnt={ca_m.get('clone_refcnt')}")

            free_before = wait_for_stable_free_space(mnt_path)
            os.unlink(clone_a)
            free_mid = wait_for_stable_free_space(mnt_path)
            print(f"Freed by deleting mutated: {free_mid - free_before} bytes")
            os.unlink(orig_a)
            free_end = wait_for_stable_free_space(mnt_path)
            print(f"Freed by deleting orig: {free_end - free_mid} bytes")

            # Subcase B
            print("\n  Subcase B: Delete ORIGINAL first")
            c3b_dir = os.path.join(mnt_path, "case3b")
            os.makedirs(c3b_dir)
            orig_b = os.path.join(c3b_dir, "orig")
            clone_b = os.path.join(c3b_dir, "clone")

            with open(orig_b, "wb") as f:
                f.write(os.urandom(2 * 1024 * 1024))

            run_cmd(["cp", "-c", orig_b, clone_b])

            with open(clone_b, "r+b") as f:
                f.seek(1024 * 1024)
                f.write(os.urandom(512 * 1024))

            free_before = wait_for_stable_free_space(mnt_path)
            os.unlink(orig_b)
            free_mid = wait_for_stable_free_space(mnt_path)
            print(f"Freed by deleting orig: {free_mid - free_before} bytes")
            os.unlink(clone_b)
            free_end = wait_for_stable_free_space(mnt_path)
            print(f"Freed by deleting mutated: {free_end - free_mid} bytes")

            # === Case 4: Snapshots ===
            print("\n--- Case 4: Snapshots ---")
            print("SKIPPED — safe arbitrary APFS snapshot creation is not available through the stock CLI path used by this probe.")

        finally:
            print(f"\nCleaning up: Detaching {dev_node}")
            try:
                run_cmd(["hdiutil", "detach", dev_node])
            except subprocess.CalledProcessError:
                print("Normal detach failed, trying force detach...")
                run_cmd(["hdiutil", "detach", dev_node, "-force"])

if __name__ == "__main__":
    main()
