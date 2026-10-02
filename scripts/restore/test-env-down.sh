#!/usr/bin/env bash
# Removes everything test-env-up.sh created. Safe to run at any stage, including after a
# half-finished setup or restore; anything already gone is skipped.
set -euo pipefail
source "$(dirname "$0")/test-env-common.sh"

# a setup that failed while backing up can leave its clone and snapshots, which would stop
# the driver deleting the test disks
carina_root <<SCRIPT
if zfs list $TEST_DATASET/clone >/dev/null 2>&1; then zfs destroy $TEST_DATASET/clone; fi
zfs list -H -t snapshot -o name -r main | { grep '@restore-test\$' || true; } | while read -r s; do zfs destroy "\$s"; done
SCRIPT

test_pvs() {
    kube get pv -o json | jq -r --arg ns "$TEST_NAMESPACE" '.items[] | select(.spec.claimRef.namespace == $ns) | .metadata.name'
}

# Restored-but-not-redeployed disks are reserved for claims that don't exist, so nothing
# would ever delete them. Redeploying lets them bind, and then the driver removes them
# along with the app, the same way it removes any disk.
if [ -n "$(test_pvs)" ] && ! kube get namespace "$TEST_NAMESPACE" >/dev/null 2>&1; then
    step "binding restored disks so the driver can remove them"
    kube apply -f - < "$HERE/test-app.yaml" >/dev/null
    kube wait -n "$TEST_NAMESPACE" --for=condition=Ready pod/holder --timeout=240s >/dev/null
fi

if kube get namespace "$TEST_NAMESPACE" >/dev/null 2>&1; then
    step "deleting the test app and its disks"
    kube delete namespace "$TEST_NAMESPACE" --wait=true --timeout=180s >/dev/null
    until [ -z "$(test_pvs)" ]; do sleep 2; done
fi

has_dataset() { [ "$(midclt pool.dataset.query "[[\"name\",\"=\",\"$1\"]]")" != "[]" ]; }

if has_dataset "$TEST_DATASET"; then
    # a restore that failed midway, or was never registered, can leave disks the driver doesn't know about
    step "sweeping leftover test disks and shares"
    carina bash -s <<SCRIPT
for handle in \$(cat /mnt/$TEST_DATASET/handles.txt 2>/dev/null); do
    for id in \$(midclt call iscsi.target.query | jq --arg h "\$handle" '.[] | select(.alias == "CSI volume " + \$h) | .id'); do
        midclt call iscsi.target.delete \$id true >/dev/null && echo "  removed iscsi target for \$handle"
    done
    for id in \$(midclt call iscsi.extent.query | jq --arg h "\$handle" '.[] | select(.disk == "zvol/" + \$h) | .id'); do
        midclt call iscsi.extent.delete \$id >/dev/null && echo "  removed iscsi extent for \$handle"
    done
    for id in \$(midclt call sharing.nfs.query | jq --arg p "/mnt/\$handle" '.[] | select(.path == \$p) | .id'); do
        midclt call sharing.nfs.delete \$id >/dev/null && echo "  removed nfs export for \$handle"
    done
    if [ "\$(midclt call pool.dataset.query "[[\"name\",\"=\",\"\$handle\"]]")" != "[]" ]; then
        midclt call pool.dataset.delete "\$handle" '{"recursive":true}' >/dev/null && echo "  removed disk \$handle"
    fi
done
SCRIPT

    step "removing $TEST_DATASET"
    carina "midclt call pool.dataset.delete $TEST_DATASET '{\"recursive\":true}'" >/dev/null
fi

step "deleting R2 bucket $TEST_BUCKET"
r2 "$TEST_BUCKET" <<'PY'
bucket = sys.argv[1]
if bucket not in [b["Name"] for b in c.list_buckets()["Buckets"]]:
    sys.exit(print("  already gone"))
keys = [o["Key"] for page in c.get_paginator("list_objects_v2").paginate(Bucket=bucket) for o in page.get("Contents", [])]
for i in range(0, len(keys), 1000):
    c.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": True})
c.delete_bucket(Bucket=bucket)
print(f"  deleted {len(keys)} objects and the bucket")
PY

echo -e "\nTest environment removed."
