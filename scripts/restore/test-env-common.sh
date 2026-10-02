# Shared settings for the restore test scripts. Sourced, not run.

# The test gets its own bucket so it can never touch real backups; teardown deletes it.
TEST_BUCKET=andromeda-restore-test
TEST_NAMESPACE=restore-test
TEST_DATASET=main/restore-test-meta

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)

# everything else comes from the Ansible, the same way restore_k3s_volumes.py gets it
# stderr goes to a file: ansible refuses to start if any of its streams is non-blocking
ansible_errors=$(mktemp)
SETTINGS=$(cd "$REPO" && ANSIBLE_STDOUT_CALLBACK=json .venv/bin/ansible-playbook -i inventories/prod \
    --tags restore-settings "$HERE/settings.yml" </dev/null 2>"$ansible_errors") || true
if ! jq -e '.plays[0].tasks[-1].hosts[] | .failed | not' <<<"$SETTINGS" >/dev/null 2>&1; then
    echo "could not read the settings from Ansible:" >&2
    jq -r '.plays[0].tasks[-1].hosts[].msg' <<<"$SETTINGS" >&2 2>/dev/null || tail -20 "$ansible_errors" >&2
    rm -f "$ansible_errors"
    exit 1
fi
rm -f "$ansible_errors"
SETTINGS=$(jq -c '.plays[0].tasks[-1].hosts[].msg' <<<"$SETTINGS")
setting() { jq -r ".$1" <<<"$SETTINGS"; }

CARINA=$(setting carina.user)@$(setting carina.address)
CONTROL_PLANE=$(setting control_plane.user)@$(setting control_plane.address)
MANIFEST_FOLDER=$(setting volume_map_folder)
SUDO_PASSWORD=$(setting carina.sudo_password)

carina() { ssh -o BatchMode=yes "$CARINA" "$@"; }
# ssh joins its arguments into one string, so kube quotes each one to keep them intact
kube() { ssh -o BatchMode=yes "$CONTROL_PLANE" "$(printf '%q ' kubectl "$@")"; }

# midclt METHOD [JSON]; the argument travels over stdin so no shell quoting is involved
midclt() {
    if [ $# -gt 1 ]; then
        printf '%s' "$2" | carina "midclt call $1 \"\$(cat)\""
    else
        carina "midclt call $1"
    fi
}

# runs the script on stdin as root on carina; the sudo password goes over stdin, never argv
carina_root() {
    { printf '%s\n' "$SUDO_PASSWORD"; cat; } |
        carina 'read -r P; S=$(cat); printf "%s\n" "$P" | sudo -S -p "" bash -euo pipefail -c "$S"'
}

step() { printf '\n== %s\n' "$*"; }

# prints the data the test app holds; identical before deletion and after restore means it worked
CHECK_DATA='cd /block && md5sum random.bin sub/note.txt && stat -c "%u:%g block/%n" sub/note.txt
cd /nfs && md5sum random.bin sub/note.txt && stat -c "%u:%g nfs/%n" sub/note.txt'

# write_root PATH < content -- writes a file as root on carina. The content is streamed,
# since sudo rejects command lines much past a few KB.
write_root() {
    { printf '%s\n' "$SUDO_PASSWORD"; cat; } |
        carina "read -r P; F=\$(mktemp); cat > \$F; printf '%s\n' \"\$P\" | sudo -S -p '' install -m 644 \$F $(printf %q "$1"); rm -f \$F"
}

# r2 ARG... < python -- runs the python on stdin on carina with `c`, an S3 client for R2.
# The keys travel over stdin; the code in argv holds no secrets.
r2() {
    { printf '%s\n' "$(setting endpoint)" "$(setting access_key_id)" "$(setting secret_access_key)"
      printf '%s\n' 'import boto3, os, sys' \
          'c = boto3.client("s3", endpoint_url=os.environ["E"], aws_access_key_id=os.environ["K"],' \
          '                 aws_secret_access_key=os.environ["S"], region_name="auto")'
      cat; } | carina "read -r E; read -r K; read -r S; export E K S; python3 -c \"\$(cat)\" $*"
}

# restic_root RESTIC_PASSWORD < script -- runs the script as root on carina with restic's
# settings for the test bucket in its environment, and $REPO as the bucket's address.
# Secrets travel over stdin and arrive as environment variables, never in a command line.
restic_root() {
    { printf '%s\n' "$SUDO_PASSWORD" "$(setting access_key_id)" "$(setting secret_access_key)" "$1"
      printf '%s\n' "s3:$(setting endpoint | sed 's:/*$::')/$TEST_BUCKET"
      cat; } | carina 'read -r P; read -r AWS_ACCESS_KEY_ID; read -r AWS_SECRET_ACCESS_KEY; read -r RESTIC_PASSWORD; read -r REPO
        export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY RESTIC_PASSWORD REPO
        S=$(cat); printf "%s\n" "$P" | sudo -S -p "" -E bash -euo pipefail -c "$S"'
}
