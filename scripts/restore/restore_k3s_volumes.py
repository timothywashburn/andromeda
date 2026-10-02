#!/usr/bin/env python3
"""Restore k3s volumes from the nightly R2 backups made by s3-backup.py.

Brings back the disks the TrueNAS CSI driver created, under their original names, then
re-registers them with Kubernetes so each app gets its old disk back when it deploys.

Run it after rebuilding with Ansible and BEFORE GitOps deploys the apps -- an app that
deploys first is given a new empty disk instead.

    .venv/bin/python scripts/restore/restore_k3s_volumes.py [--before 2026-09-20] [--dry-run]
"""
import argparse
import base64
import curses
import getpass
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is missing -- run this with the repo's venv: .venv/bin/python scripts/restore/restore_k3s_volumes.py")

REPO = Path(__file__).resolve().parents[2]
SETTINGS_PLAYBOOK = Path(__file__).resolve().parent / "settings.yml"
VOLUME_MAP_FILE = "/volume-map.yaml"  # written by roles/k3s/templates/k3s-volume-map.sh.j2
CSI_DRIVER = "csi.truenas.io"


# Runs on carina. stdin carries the sudo password, base64 "KEY=value" lines, then the
# script, so secrets travel as environment variables and never appear in a process list.
CARINA_WRAPPER = r"""
IFS= read -r sudo_password
IFS= read -r env_b64
script=$(cat)
while IFS= read -r kv; do [ -n "$kv" ] && export "$kv"; done < <(printf %s "$env_b64" | base64 -d)
if [ "$AS_ROOT" = 1 ]; then
    printf '%s\n' "$sudo_password" | sudo -S -p '' -E bash -euo pipefail -c "$script"
else
    bash -euo pipefail -c "$script"
fi
"""


class RemoteError(Exception):
    pass


@dataclass
class Host:
    address: str
    user: str

    def run(self, command, stdin=""):
        proc = subprocess.run(["ssh", "-o", "BatchMode=yes", f"{self.user}@{self.address}", command],
                              input=stdin, capture_output=True, text=True)
        if proc.returncode:
            raise RemoteError(proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}")
        return proc.stdout


class Carina:
    def __init__(self, host, sudo_password):
        self.host = host
        self.sudo_password = sudo_password
        self.env = {}

    def sh(self, script, root=False):
        env = "".join(f"{k}={v}\n" for k, v in {**self.env, "AS_ROOT": int(root)}.items())
        stdin = "\n".join([self.sudo_password if root else "", base64.b64encode(env.encode()).decode(), script])
        return self.host.run("bash -c " + shlex.quote(CARINA_WRAPPER), stdin)

    def midclt(self, method, *args):
        out = self.sh(shlex.join(["midclt", "call", method, *(json.dumps(a) for a in args)])).strip()
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return out  # midclt prints bare values such as True as text


def restic_cmd(folder, *args):
    return f'restic --no-cache -r "$REPO"{shlex.quote(folder)} {shlex.join(args)}'


# --- backups -------------------------------------------------------------------------

def parse_time(value):
    return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", value))


def list_snapshots(carina, folders):
    """Map each folder to its restic snapshots; a folder with no repository maps to []."""
    script = f"""
for f in {" ".join(shlex.quote(f) for f in folders)}; do
    out=$(restic --no-cache -r "$REPO$f" snapshots --json 2>&1) && rc=0 || rc=$?
    printf '%s %s %s\\n' "$rc" "$f" "$(printf %s "$out" | base64 -w0)"
done
"""
    result = {}
    for line in carina.sh(script).splitlines():
        rc, folder, payload = (line.split(" ", 2) + [""])[:3]
        text = base64.b64decode(payload).decode()
        if rc == "0":
            result[folder] = json.loads(text)
        elif "wrong password" in text:
            sys.exit("Wrong restic password.")
        elif "Is there a repository" in text:
            result[folder] = []
        else:
            raise RemoteError(f"reading backups in {folder}: {text.strip()}")
    return result


def pick(snapshots, before):
    eligible = [s for s in snapshots if before is None or parse_time(s["time"]) <= before]
    return max(eligible, key=lambda s: parse_time(s["time"]), default=None)


def local(timestamp):
    return parse_time(timestamp).astimezone().strftime("%Y-%m-%d %H:%M")


# --- volumes -------------------------------------------------------------------------

UNITS = {"": 1, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12,
         "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}


def to_bytes(quantity):
    number, unit = re.fullmatch(r"(\d+)([KMGT]i?)?", quantity).groups()
    return int(number) * UNITS[unit or ""]


@dataclass
class Volume:
    pv: dict
    snapshot: dict = None
    blocked: str = None
    selected: bool = False
    size: int = None

    @property
    def name(self):
        return self.pv["metadata"]["name"]

    @property
    def claim(self):
        ref = self.pv["spec"]["claimRef"]
        return f'{ref["namespace"]}/{ref["name"]}'

    @property
    def handle(self):
        return self.pv["spec"]["csi"]["volumeHandle"]

    @property
    def attrs(self):
        return self.pv["spec"]["csi"].get("volumeAttributes", {})

    @property
    def protocol(self):
        return self.attrs.get("protocol", "?")

    @property
    def capacity(self):
        return self.pv["spec"]["capacity"]["storage"]

    @property
    def folder(self):
        return "/" + self.handle  # the handle is the dataset's full name


def volumes_from(manifest):
    pvs = [o for o in manifest["items"] if o["kind"] == "PersistentVolume"
           and o["spec"].get("csi", {}).get("driver") == CSI_DRIVER and o["spec"].get("claimRef")]
    return sorted((Volume(pv) for pv in pvs), key=lambda v: v.claim)


def check(volumes, carina, kube, before):
    """Pick each volume's backup and mark anything that must not be restored, with why."""
    existing = {d["name"] for d in carina.midclt("pool.dataset.query", [["name", "in", [v.handle for v in volumes]]])}
    cluster = json.loads(kube.run("kubectl get pv,pvc -A -o json"))["items"]
    pvs = {o["metadata"]["name"] for o in cluster if o["kind"] == "PersistentVolume"}
    pvcs = {f'{o["metadata"]["namespace"]}/{o["metadata"]["name"]}'
            for o in cluster if o["kind"] == "PersistentVolumeClaim"}
    basename = carina.midclt("iscsi.global.config")["basename"]
    snapshots = list_snapshots(carina, [v.folder for v in volumes])

    for v in volumes:
        v.snapshot = pick(snapshots[v.folder], before)
        if v.protocol not in ("iscsi", "nfs"):
            v.blocked = f"unsupported protocol {v.protocol}"
        elif not v.snapshot and before and snapshots[v.folder]:
            v.blocked = "no backup before that date"
        elif not v.snapshot:
            # s3-backup.py skips empty filesystems, since restic refuses an empty backup
            v.blocked = "no backup" + (" -- it was empty, or is newer than the last backup" if v.protocol == "nfs" else "")
        elif v.handle in existing:
            v.blocked = "disk already exists on TrueNAS"
        elif v.name in pvs:
            v.blocked = "already registered in Kubernetes"
        elif v.claim in pvcs:
            v.blocked = "app already deployed -- delete its claim first"
        elif v.protocol == "iscsi" and not v.attrs["targetIQN"].startswith(basename + ":"):
            v.blocked = f"iSCSI name prefix is now {basename}"


# --- selection screen ----------------------------------------------------------------

HELP = "up/down move   space toggle   a all   n none   enter continue   q quit"


def choose(volumes, title, subtitle):
    """Return the chosen volumes, or None if the user quit."""
    os.environ.setdefault("ESCDELAY", "25")
    for v in volumes:
        v.selected = v.blocked is None
    width = max(len(v.claim) for v in volumes)
    rows = [f"{v.claim:<{width}}  {v.protocol:<5}  {v.capacity:>6}  "
            + (v.blocked if v.blocked else f"backup {local(v.snapshot['time'])}") for v in volumes]
    return curses.wrapper(_selection_screen, volumes, rows, title, subtitle)


def _put(screen, y, text, attr, width):
    try:
        screen.addnstr(y, 0, text, width - 1, attr)
    except curses.error:
        pass


def _selection_screen(screen, volumes, rows, title, subtitle):
    curses.curs_set(0)
    cursor = top = 0
    while True:
        height, width = screen.getmaxyx()
        visible = max(1, height - 5)
        top = max(min(top, cursor), cursor - visible + 1)
        screen.erase()
        _put(screen, 0, f" {title}", curses.A_BOLD, width)
        _put(screen, 1, f" {subtitle}", curses.A_DIM, width)
        for y, i in enumerate(range(top, min(top + visible, len(volumes))), start=3):
            v = volumes[i]
            mark = "[-]" if v.blocked else "[x]" if v.selected else "[ ]"
            attr = (curses.A_DIM if v.blocked else curses.A_NORMAL) | (curses.A_REVERSE if i == cursor else 0)
            _put(screen, y, f" {mark} {rows[i]}", attr, width)
        chosen = sum(v.selected for v in volumes)
        _put(screen, height - 1, f" {HELP}    {chosen} selected", curses.A_DIM, width)
        screen.refresh()

        key = screen.getch()
        if key in (curses.KEY_UP, ord("k")):
            cursor = max(cursor - 1, 0)
        elif key in (curses.KEY_DOWN, ord("j")):
            cursor = min(cursor + 1, len(volumes) - 1)
        elif key == ord(" ") and not volumes[cursor].blocked:
            volumes[cursor].selected = not volumes[cursor].selected
        elif key in (ord("a"), ord("n")):
            for v in volumes:
                v.selected = key == ord("a") and not v.blocked
        elif key in (curses.KEY_ENTER, 10, 13):
            return [v for v in volumes if v.selected]
        elif key in (ord("q"), 27):
            return None


# --- restore steps -------------------------------------------------------------------

def create_disk(v, carina, undo):
    parts = v.handle.split("/")
    parents = ["/".join(parts[:i]) for i in range(2, len(parts))]
    existing = {d["name"] for d in carina.midclt("pool.dataset.query", [["name", "in", parents]])}
    for parent in parents:
        if parent not in existing:
            carina.midclt("pool.dataset.create", {"name": parent, "type": "FILESYSTEM"})

    if v.protocol == "iscsi":
        listing = carina.sh(restic_cmd(v.folder, "ls", "--json", v.snapshot["id"], "/volume"))
        v.size = next(n["size"] for n in map(json.loads, listing.splitlines()) if n.get("path") == "/volume")
        disk = {"name": v.handle, "type": "VOLUME", "volsize": v.size}
        if v.attrs.get("sparse") == "true":
            disk["sparse"] = True
        if "volblocksize" in v.attrs:
            disk["volblocksize"] = v.attrs["volblocksize"]
    else:
        disk = {"name": v.handle, "type": "FILESYSTEM", "refquota": to_bytes(v.capacity)}
    if "compression" in v.attrs:
        disk["compression"] = v.attrs["compression"]
    carina.midclt("pool.dataset.create", disk)
    undo.append(lambda: carina.midclt("pool.dataset.delete", v.handle, {"recursive": True}))


def restore_data(v, carina):
    snapshot = v.snapshot["id"]
    if v.protocol == "iscsi":
        # dd refuses to write if the device never appeared, rather than creating a plain file in /dev
        script = f"""
dev={shlex.quote("/dev/zvol/" + v.handle)}
for i in $(seq 30); do [ -b "$dev" ] && break; sleep 1; done
[ -b "$dev" ] || {{ echo "disk device never appeared" >&2; exit 3; }}
{restic_cmd(v.folder, "dump", snapshot, "/volume")} | dd of="$dev" bs=1M conv=sparse iflag=fullblock status=none
if cmp -s -n {v.size} "$dev" /dev/zero; then echo "restored disk is blank" >&2; exit 3; fi
"""
    else:
        target = v.attrs.get("nfsPath", "/mnt/" + v.handle)
        script = f"""
{restic_cmd(v.folder, "restore", snapshot, "--target", target)} >/dev/null
expected=$({restic_cmd(v.folder, "ls", "--json", snapshot)} | jq -s '[.[] | select(.struct_type == "node" and .type == "file")] | length')
actual=$(find {shlex.quote(target)} -type f | wc -l)
[ "$actual" -eq "$expected" ] || {{ echo "restored $actual files, backup has $expected" >&2; exit 3; }}
"""
    carina.sh(script, root=True)


def create_share(v, carina, undo):
    """Recreate the share exactly as the CSI driver makes it, so the saved record points at it."""
    if v.protocol == "iscsi":
        portal_ip = v.attrs["targetPortal"].rsplit(":", 1)[0]
        portal = next(p for p in carina.midclt("iscsi.portal.query")
                      if any(listen["ip"] == portal_ip for listen in p["listen"]))
        target = carina.midclt("iscsi.target.create", {
            "name": v.attrs["targetIQN"].split(":", 1)[1],
            "alias": f"CSI volume {v.handle}",
            "groups": [{"portal": portal["id"]}],
        })
        undo.append(lambda: carina.midclt("iscsi.target.delete", target["id"]))
        # TrueNAS caps extent names at 64 characters; the driver cuts the handle off there too
        extent = carina.midclt("iscsi.extent.create", {"name": v.handle[:64], "disk": f"zvol/{v.handle}"})
        undo.append(lambda: carina.midclt("iscsi.extent.delete", extent["id"]))
        link = carina.midclt("iscsi.targetextent.create",
                             {"target": target["id"], "extent": extent["id"], "lunid": int(v.attrs["lun"])})
        undo.append(lambda: carina.midclt("iscsi.targetextent.delete", link["id"]))
    else:
        share = carina.midclt("sharing.nfs.create", {
            "path": v.attrs.get("nfsPath", "/mnt/" + v.handle),
            "comment": f"CSI volume {v.handle}",
            "mapall_user": "root",
            "mapall_group": "wheel",
        })
        undo.append(lambda: carina.midclt("sharing.nfs.delete", share["id"]))


def register(v, kube, undo):
    """Re-create the volume record, reserved for its claim by name (the claim's old ID is dropped)."""
    meta = v.pv["metadata"]
    spec = dict(v.pv["spec"])
    spec["claimRef"] = {k: spec["claimRef"][k] for k in ("apiVersion", "kind", "namespace", "name")
                        if k in spec["claimRef"]}
    record = {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {k: meta[k] for k in ("name", "labels", "annotations") if k in meta},
        "spec": spec,
    }
    kube.run("kubectl create -f -", json.dumps(record))
    undo.append(lambda: kube.run(f"kubectl delete pv {shlex.quote(v.name)}"))
    phase = kube.run(f"kubectl get pv {shlex.quote(v.name)} -o jsonpath={{.status.phase}}").strip()
    if phase != "Available":
        raise RemoteError(f"volume record is {phase or 'missing'}, expected Available")


STEPS = [
    ("creating disk", lambda v, c, k, u: create_disk(v, c, u)),
    ("restoring data", lambda v, c, k, u: restore_data(v, c)),
    ("recreating share", lambda v, c, k, u: create_share(v, c, u)),
    ("registering with Kubernetes", lambda v, c, k, u: register(v, k, u)),
]


def restore(v, carina, kube):
    """Run each step; on failure, undo what this run created for the volume so it can be retried."""
    undo = []
    for label, step in STEPS:
        print(f"    {label:<30}", end="", flush=True)
        try:
            step(v, carina, kube, undo)
        except (RemoteError, StopIteration, KeyError) as e:
            print("FAILED")
            print(f"      {e}")
            break
        print("ok")
    else:
        return True

    print(f"    {'undoing':<30}", end="", flush=True)
    leftovers = []
    for action in reversed(undo):
        try:
            action()
        except Exception as e:
            leftovers.append(str(e))
    if leftovers:
        print("INCOMPLETE -- remove these by hand before retrying:")
        for message in leftovers:
            print(f"      {message}")
    else:
        print("ok -- safe to retry")
    return False


# --- setup ---------------------------------------------------------------------------

def load_settings():
    """Hosts, R2 settings and the volume list's location, as the Ansible resolves them."""
    proc = subprocess.run(
        [str(REPO / ".venv/bin/ansible-playbook"), "-i", "inventories/prod", "--tags", "restore-settings",
         str(SETTINGS_PLAYBOOK)],
        cwd=REPO, env={**os.environ, "ANSIBLE_STDOUT_CALLBACK": "json"},
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    try:
        result = next(iter(json.loads(proc.stdout)["plays"][0]["tasks"][-1]["hosts"].values()))
    except (json.JSONDecodeError, KeyError, IndexError, StopIteration):
        sys.exit(f"Could not run {SETTINGS_PLAYBOOK.name}:\n{proc.stdout[-2000:] or proc.stderr[-2000:]}")
    if result.get("failed"):
        sys.exit(f"Could not read the settings from Ansible: {result['msg']}")
    return result["msg"]


def parse_before(value):
    moment = datetime.fromisoformat(value)
    if len(value) == 10:
        moment = datetime.combine(moment.date(), time.max)
    return moment.astimezone()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--before", type=parse_before, metavar="DATE",
                        help="use the newest backups taken on or before DATE (YYYY-MM-DD or ISO time)")
    parser.add_argument("--dry-run", action="store_true", help="show what would be restored, change nothing")
    parser.add_argument("--bucket", help="R2 bucket to restore from (default: the one the backups use)")
    args = parser.parse_args()

    settings = load_settings()
    bucket = args.bucket or settings["bucket"]
    storage = Host(settings["carina"]["address"], settings["carina"]["user"])
    control_plane = Host(settings["control_plane"]["address"], settings["control_plane"]["user"])
    carina = Carina(storage, settings["carina"]["sudo_password"])
    restic_password = getpass.getpass("restic password: ")

    print("Reading backups from R2...")
    carina.env = {
        "AWS_ACCESS_KEY_ID": settings["access_key_id"],
        "AWS_SECRET_ACCESS_KEY": settings["secret_access_key"],
        "RESTIC_PASSWORD": restic_password,
        "REPO": f"s3:{settings['endpoint'].rstrip('/')}/{bucket}",
    }

    map_folder = settings["volume_map_folder"]
    manifest_snapshot = pick(list_snapshots(carina, [map_folder])[map_folder], args.before)
    if not manifest_snapshot:
        sys.exit(f"No volume list in {bucket}{map_folder}" + (" before that date." if args.before else "."))
    manifest = yaml.safe_load(carina.sh(restic_cmd(map_folder, "dump", manifest_snapshot["id"], VOLUME_MAP_FILE)))
    volumes = volumes_from(manifest)
    if not volumes:
        sys.exit("The volume list has no TrueNAS volumes in it.")
    check(volumes, carina, control_plane, args.before)

    subtitle = f"bucket {bucket}   volume list from {local(manifest_snapshot['time'])}"
    if args.before:
        subtitle += f"   backups on or before {args.before:%Y-%m-%d %H:%M}"
    chosen = choose(volumes, "Restore k3s volumes" + ("  (dry run)" if args.dry_run else ""), subtitle)
    if chosen is None:
        sys.exit("Cancelled.")
    if not chosen:
        sys.exit("Nothing selected.")

    print(f"\n{'Would restore' if args.dry_run else 'About to restore'} {len(chosen)} volume(s) onto {storage.address}:")
    for v in chosen:
        print(f"  {v.claim}  ({v.protocol}, {v.capacity}) from backup {local(v.snapshot['time'])}")
    if args.dry_run:
        return
    print("\nGitOps must be paused, or apps will grab new empty disks before these are registered.")
    if input('Type "yes" to continue: ').strip() != "yes":
        sys.exit("Cancelled.")

    failed = []
    for i, v in enumerate(chosen, start=1):
        print(f"\n[{i}/{len(chosen)}] {v.claim}")
        if not restore(v, carina, control_plane):
            failed.append(v.claim)

    print(f"\nRestored {len(chosen) - len(failed)} of {len(chosen)}.")
    if failed:
        print("Failed: " + ", ".join(failed))
    print("\nNext:\n"
          "  1. Resume GitOps. Each app picks up its restored disk.\n"
          "  2. Check the apps have their data.\n"
          "Nightly backups pick the restored disks up on their own, into the same R2 folders.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    try:
        main()
    except RemoteError as e:
        sys.exit(f"\n{e}")
    except KeyboardInterrupt:
        sys.exit("\nInterrupted -- the volume in progress may be partly restored.")
