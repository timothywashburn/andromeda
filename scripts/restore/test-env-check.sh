#!/usr/bin/env bash
# Run after restore_k3s_volumes.py: redeploys the test app the way GitOps would and checks
# it came back with exactly the data it had before it was deleted.
set -euo pipefail
source "$(dirname "$0")/test-env-common.sh"

step "redeploying the test app"
kube apply -f - < "$HERE/test-app.yaml" >/dev/null
kube wait -n "$TEST_NAMESPACE" --for=condition=Ready pod/holder --timeout=240s >/dev/null
kube get pvc -n "$TEST_NAMESPACE" -o custom-columns=CLAIM:.metadata.name,STATUS:.status.phase,DISK:.spec.volumeName

step "comparing data"
expected=$(carina cat "/mnt/$TEST_DATASET/expected.txt")
actual=$(kube exec -n "$TEST_NAMESPACE" holder -- sh -c "$CHECK_DATA")
if diff <(echo "$expected") <(echo "$actual"); then
    echo "$actual"
    echo -e "\nPASS -- identical to before the app was deleted"
else
    echo -e "\nFAIL -- lines marked < are expected, > are what came back"
    exit 1
fi
