# Restoring k3s volumes from the R2 backups

**NOTE: THE FOLLOWING IS ALL AI SLOP**

`restore_k3s_volumes.py` brings back the disks the TrueNAS CSI driver made, under their
original names, and re-registers them with Kubernetes so each app finds its old disk when
it deploys. The other three scripts build and remove a throwaway test for it.

The backups themselves are made by `s3-backup.py`, which the Ansible in
`roles/truenas/tasks/backups.yml` installs on carina and runs nightly from a TrueNAS cron
job.

---

## How the backups are laid out

- **Everything under `main/protected` is backed up; nothing under `main/unprotected` is.**
  The script takes one recursive snapshot, so every disk is backed up from the same moment.
- **One restic repo per dataset or zvol with no children of its own**, named after its
  full dataset name. For a k3s disk that is its volume handle:
  ```
  main/protected/k3s-data/iscsi/pvc-84f1…  →  andromeda-test/main/protected/k3s-data/iscsi/pvc-84f1…
  main/protected/k3s-volume-metadata       →  andromeda-test/main/protected/k3s-volume-metadata
  ```
  Parents are skipped because a parent's snapshot shows its children as empty folders.
- **A block disk is one file, `/volume`**: the raw disk, read from a clone of the snapshot.
  **A folder is backed up with its files at the root** of the backup.
- **Empty folders are skipped**, since restic refuses an empty backup. An NFS volume with
  no backup was empty, or is newer than the last run.
- **Retention:** 7 daily, 5 weekly, 12 monthly per repo, applied after each backup.

---

## Why a restore is not automatic

Each app's storage has two records:

- **the claim** — the app's request ("mariadb needs 5 GB"), which lives in GitOps
- **the volume** — the address of a real disk on carina, and which claim it belongs to

On a normal deploy the claim is unanswered, so the driver makes a **new empty disk**. That
is exactly what you do not want during a restore. The volume records say which R2 folder
belongs to which app, and reserve each restored disk for its claim by name, so the app
picks up its own data instead.

Those records are in the volume list (`volume-map.yaml`) that the hourly job on the
control plane writes, and which is backed up to R2 like anything else. **Without it, a
folder named `pvc-4c666862-…` is an anonymous disk image.** The volume's name comes from
the claim's internal ID, not its name, so redeploying produces different names and nothing
records that the old one was `bmc-panel-storage`.

---

## Using it after losing carina

1. **Rebuild with Ansible as usual.** Pool, k3s and the driver come back empty.
2. **Pause GitOps.** An app that deploys first is given a new empty disk, and the script
   then refuses that volume.
3. **Run the restore:**
   ```
   .venv/bin/python scripts/restore/restore_k3s_volumes.py
   ```
   It needs the repo's venv, for Ansible and PyYAML. It asks for the restic password;
   everything else comes from the Ansible (see "Settings come from the Ansible").
4. **Resume GitOps.** Each app picks up its restored disk.
5. **Check the apps have their data.**

Nothing needs re-running afterwards. Restored disks keep their original names, so the
next nightly backup carries on in the same R2 folders and the history stays unbroken.

Options: `--before 2026-09-20` uses the newest backups on or before a date (also accepts a
full timestamp), `--dry-run` shows what would happen and changes nothing, and `--bucket`
points at a different bucket, which the test scripts use.

### The selection screen

```
 [x] restore-test/data-block   iscsi   1Gi  backup 2026-09-22 00:47
 [-] bmc/minio                 iscsi  20Gi  disk already exists on TrueNAS
```

up/down (or j/k) to move, space to toggle, `a` all, `n` none, Enter to continue, `q` to
quit. Everything restorable is ticked to begin with. Rows marked `[-]` cannot be restored
and say why; they cannot be selected.

### What it does per volume

1. **Creates the disk** under its original name, with the size and settings recorded for it.
2. **Fills it from R2** — a block disk gets its image written back, an NFS one gets its
   files, with owners preserved.
3. **Checks it isn't blank.** In a real disaster there is no original to compare against,
   so this catches an empty restore but cannot prove every byte.
4. **Recreates the share** the cluster reaches the disk through, named exactly as the
   driver names it, so the saved record points at something real. (iSCSI extent names
   are cut off at 64 characters, as the driver does — TrueNAS allows no more.)
5. **Registers the disk**, reserved for its claim by name.

### When something goes wrong

It refuses rather than guessing: a disk that already exists, a claim that already exists
(GitOps wasn't paused), a volume with no backup, or a wrong restic password all stop it.

If a volume fails partway, **it undoes what it created for that volume** — and only that
volume — so you can fix the cause and run it again. Other volumes carry on. If the undo
itself cannot finish, it prints exactly what to remove by hand.

---

## Testing it

The test deploys a small app with one block disk and one NFS disk, writes known data,
backs it up, deletes the app, and lets you restore it.

```
scripts/restore/test-env-up.sh      # build it and delete the app, as if it were lost
.venv/bin/python scripts/restore/restore_k3s_volumes.py --bucket andromeda-restore-test
scripts/restore/test-env-check.sh   # redeploy the app and compare its data
scripts/restore/test-env-down.sh    # remove everything the test made
```

**The test uses its own bucket (`andromeda-restore-test`) so it can never touch real
backups**, and teardown deletes that bucket outright. It asks for a password for the test
backups; type the same one when the restore script asks.

The test cannot use `s3-backup.py` itself, which backs up everything under
`main/protected` into the real bucket. `test-env-up.sh` instead backs up just its own two
disks **the same way** — same folder names, same snapshot and clone, same restic
commands. If the backup script's layout changes, change `test-env-up.sh` to match.

`test-env-down.sh` works from any stage, including a half-finished setup or restore. Run
it before building the test again.

Bear in mind the test's disks live under `main/protected` like any other app's. While the
test exists they appear in the real volume list, and if it is still up at midnight the
nightly job backs them up into the real bucket too.

---

## Settings come from the Ansible

Nothing here repeats the Ansible. `settings.yml` is a tiny playbook that prints what the
scripts need, resolved by Ansible itself: the hosts and their users, the sudo password,
the R2 endpoint, keys and bucket, and the volume list's dataset (`k3s_metadata`). Both the
restore tool and the test scripts read it, so a renamed bucket or dataset is picked up
without touching anything here. It connects to no host: `--tags restore-settings` skips
the truenas role's tasks, and it only loads the role for its defaults.

Two things are fixed in the scripts instead:

- **The volume list's file name, `volume-map.yaml`,** written by
  `roles/k3s/templates/k3s-volume-map.sh.j2`. If that changes, change `VOLUME_MAP_FILE` in
  `restore_k3s_volumes.py` and the name in `test-env-up.sh`.
- **Where a disk's backup is:** `s3-backup.py.j2` names each repo after its dataset's full
  name, and the tool relies on that to find a disk's backup at its volume handle. If the
  backup script ever names repos differently, the `folder` property in
  `restore_k3s_volumes.py` and the backup loop in `test-env-up.sh` change with it.

---|---|
| | | Ansible |
|---|---|---|
| `BUCKET` | `andromeda-test` | `s3_backup.bucket` |
| `MANIFEST_FOLDER` | `/main/protected/k3s-volume-metadata` | `k3s_metadata.dataset` |
| `MANIFEST_FILE` | `/volume-map.yaml` | `roles/k3s/templates/k3s-volume-map.sh.j2` |
| folder per disk | `/<volume handle>` | `s3-backup.py.j2` names repos by dataset |

Change them here if the Ansible changes. The test scripts share `MANIFEST_FOLDER` through
`test-env-common.sh`.

---

## Worth knowing

- **The R2 bucket must already exist.** Create it in the Cloudflare dashboard. Backing up
  into a missing bucket has never been tested.
- **The volume list must be older than the loss.** It lives in one fixed folder and is
  replaced hourly. If backups run again on a rebuilt cluster before you restore, the latest
  list describes the new empty cluster. The screen shows the list's date; use `--before` to
  pick an earlier one.
- **Restores are crash-consistent**, like any of these backups. Databases recover the way
  they do after a power cut.
- **Not covered:** Kubernetes secrets and other cluster records (GitOps rebuilds the rest),
  and anything outside `main/protected` — notably Netbird's database, which lives on pavo.

### Tested

Against TrueNAS's old built-in backups: full cycle on both disk types, forced failures
undo cleanly and can be retried, the safety refusals, `--before`, the selection keys, and
teardown from a completed test, two half-finished setups, and a restore never redeployed.

Against the `s3-backup.py` layout (2026-10-02): full cycle on both disk types, every
checksum and file owner matched. It found the 64-character extent name limit, which
failed the block disk's share step; the undo left it clean, and the retry after the fix
restored it. The driver also deleted restored disks that had snapshots on them, as it
will once hourly snapshots exist.
