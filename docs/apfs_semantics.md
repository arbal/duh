# APFS clone and private-size semantics

Applies to: `duh` APFS accounting work. This document records one bounded
empirical probe run on golf; it is evidence for the redesign, not a universal
claim about every macOS/APFS release.

## Provenance

- Test host: golf, Apple Silicon M4, `arm64`
- OS: macOS `26.6.1`, build `25G76`
- Darwin: `25.6.0`, kernel `xnu-12377.161.13~4/RELEASE_ARM64_T6041`
- Probe source: commit `bc52196b8e57f4d04aff3e128b63101acc5fca6f`
  (`feat: add APFS metadata probe harness and plumbing`)
- Probe branch: `apfs-probe-wip`
- Probe log: `apfs-probe-golf-20260829.log`
- Log SHA-256: `442c3c1aadd0ce1ececcf1404baa590ad9ffb54f8f17696d29e7f6e88780d9f9`
- Probe configuration used `--min-free 0`, an external SQLite database, and
  isolated sparse APFS images. The images were detached during cleanup.

## Case 1: full-clone `clone_refcnt`

The probe output reported the following raw observations:

| filesystem objects | object | `clone_refcnt` |
| ---: | --- | ---: |
| 1 | file 1 (alone) | 1 |
| 2 | file 1 (with one clone) | 2 |
| 2 | file 2 (the clone) | 2 |
| 3 | file 1 (with two clones) | 3 |
| 3 | file 3 (clone 2) | 3 |

This supports the operational invariant that the count includes the object
itself for a proven full-clone family. It does not justify applying the same
interpretation to partially diverged clones.

## Case 2: hardlinks and `private_size`

Raw output for the two directory entries was:

```text
hl_1: private_size = 8.0 MiB (8,388,608 bytes), nlinks = 2
hl_2: private_size = 8.0 MiB (8,388,608 bytes), nlinks = 2
Freed by deleting hl_1: 0 bytes
Freed by deleting hl_2: 8388608 bytes
```

The same underlying inode was therefore reported as 8 MiB private through
both names, but only removal of the final name reclaimed the allocation.
`private_size` must be deduplicated through `(dev, ino)` hardlink topology and
cannot be treated as independently reclaimable per pathname.

## Case 3: partially diverged clones

Both subcases started with a 2 MiB source and a `cp -c` clone. After changing
512 KiB in one member, the scan output for subcase A was:

```text
Orig:    size_blocks=2.0 MiB (2,097,152 bytes), private_size=512.0 KiB
         (524,288 bytes), ext_flags=3 (EF_MAY_SHARE_BLOCKS), clone_id=25,
         clone_refcnt=1
Mutated: size_blocks=2.0 MiB (2,097,152 bytes), private_size=512.0 KiB
         (524,288 bytes), ext_flags=3 (EF_MAY_SHARE_BLOCKS), clone_id=27,
         clone_refcnt=1
```

### Subcase A: mutated member deleted first

```text
Freed by deleting mutated: 524288 bytes
Freed by deleting orig:    2105344 bytes
```

### Subcase B: original member deleted first

```text
Freed by deleting orig:    524288 bytes
Freed by deleting mutated: 2097152 bytes
```

The probe log does not print a second metadata row for subcase B; it prints
the deletion/reclaim results above after creating the analogous isolated
clone pair. The metadata row reproduced above is therefore the complete raw
metadata emitted by the probe, not an inferred replacement for missing output.

These results support treating each valid `private_size` component as useful
guaranteed credit, subject to hardlink topology, while leaving the shared
remainder of a partial-clone family uncertain until extent-level sharing can
be established.

## Case 4: snapshots

```text
SKIPPED — safe arbitrary APFS snapshot creation is not available through the
stock CLI path used by this probe.
```

Snapshot-retained bytes were not measured. Consequently, shared clone-family
credit must not be labeled guaranteed when a snapshot could retain it.

## Design consequences

These observations are inputs to the v4 accounting redesign:

- classify allocation semantics before resolving topology;
- count hardlink-backed private bytes once, at the hardlink family LCA;
- use the includes-self `clone_refcnt` rule only for consistent, proven
  full-clone families;
- classify partial-clone private components as guaranteed and their shared
  remainder as uncertain;
- keep snapshot-affected shared allocation out of guaranteed accounting;
- do not describe `guaranteed + conditional_shared` as exactly equal to the
  result of `rm -rf` as observed by `df`.

Full runtime evidence remains in the probe log identified above. No API keys,
credentials, prompts, or unrelated private infrastructure data are included
in this document.
