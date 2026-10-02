#!/usr/bin/env bash
# Builds a throwaway test for restore_k3s_volumes.py: deploys a small app with one block
# disk and one NFS disk, writes known data, backs both up to a test-only R2 bucket, then
# deletes the app as if it had been lost. test-env-down.sh undoes all of it.
set -euo pipefail
source "$(dirname "$0")/test-env-common.sh"

# asked before anything else: ssh reads stdin and would swallow typed input
read -rsp "restic password for the test backups (the restore script will ask for it again): " restic_password; echo

step "checking prerequisites"
if kube get namespace "$TEST_NAMESPACE" >/dev/null 2>&1 || [ "$(midclt pool.dataset.query "[[\"name\",\"=\",\"$TEST_DATASET\"]]")" != "[]" ]; then
    echo "a test environment already exists -- run test-env-down.sh first"; exit 1
fi

# outside main/protected, so the nightly backup never picks it up
step "creating $TEST_DATASET (holds the volume list and test state)"
midclt pool.dataset.create "{\"name\":\"$TEST_DATASET\",\"type\":\"FILESYSTEM\"}" >/dev/null

step "deploying the test app"
kube apply -f - < "$HERE/test-app.yaml" >/dev/null
kube wait -n "$TEST_NAMESPACE" --for=condition=Ready pod/holder --timeout=240s >/dev/null

step "writing known data"
kube exec -n "$TEST_NAMESPACE" holder -- sh -c 'for d in /block /nfs; do
    mkdir -p $d/sub
    head -c 20971520 /dev/urandom > $d/random.bin
    echo hello > $d/sub/note.txt
    chown 1234:5678 $d/sub/note.txt
done; sync'
kube exec -n "$TEST_NAMESPACE" holder -- sh -c "$CHECK_DATA" | tee /dev/stderr | write_root "/mnt/$TEST_DATASET/expected.txt"

step "saving the volume list"
kube get pv,pvc -A -o yaml | write_root "/mnt/$TEST_DATASET/volume-map.yaml"
handles=$(kube get pv -o json | jq -r --arg ns "$TEST_NAMESPACE" '.items[] | select(.spec.claimRef.namespace == $ns) | .spec.csi.volumeHandle')
printf '%s\n' "$handles" | write_root "/mnt/$TEST_DATASET/handles.txt"

step "creating R2 bucket $TEST_BUCKET"
# TrueNAS cannot create buckets for S3 providers, so this is done directly
r2 "$TEST_BUCKET" <<'PY'
c.create_bucket(Bucket=sys.argv[1])
PY

# The nightly script backs up everything under main/protected into the real bucket, so the
# test does the same for just its own disks, the same way: repo named after the dataset,
# zvols read from a clone of a snapshot as "volume", filesystems as "." from a snapshot.
# Keep in step with roles/truenas/templates/s3-backup.py.j2.
step "backing up to R2 bucket $TEST_BUCKET"
restic_root "$restic_password" <<SCRIPT
for handle in $(echo $handles); do
    restic -r "\$REPO/\$handle" init >/dev/null
    zfs snapshot "\$handle@restore-test"
    if [ "\$(zfs get -H -o value type "\$handle")" = volume ]; then
        clone=$TEST_DATASET/clone
        zfs clone "\$handle@restore-test" "\$clone"
        for i in \$(seq 30); do [ -b "/dev/zvol/\$clone" ] && break; sleep 1; done
        restic -r "\$REPO/\$handle" backup --group-by "" --stdin --stdin-filename volume < "/dev/zvol/\$clone" >/dev/null
        zfs destroy "\$clone"
    else
        (cd "/mnt/\$handle/.zfs/snapshot/restore-test" && restic -r "\$REPO/\$handle" backup --group-by "" . >/dev/null)
    fi
    zfs destroy "\$handle@restore-test"
    echo "  \$handle"
done
restic -r "\$REPO$MANIFEST_FOLDER" init >/dev/null
(cd /mnt/$TEST_DATASET && restic -r "\$REPO$MANIFEST_FOLDER" backup --group-by "" . >/dev/null)
echo "  volume list"
SCRIPT

step "deleting the test app (simulating its loss)"
kube delete namespace "$TEST_NAMESPACE" --wait=true --timeout=180s >/dev/null
until [ "$(kube get pv -o json | jq --arg ns "$TEST_NAMESPACE" '[.items[] | select(.spec.claimRef.namespace == $ns)] | length')" = 0 ]; do sleep 2; done

cat <<NEXT

Ready. Now:
  .venv/bin/python scripts/restore/restore_k3s_volumes.py --bucket $TEST_BUCKET
  scripts/restore/test-env-check.sh     # redeploys the app and compares its data
  scripts/restore/test-env-down.sh      # removes everything the test created
NEXT
