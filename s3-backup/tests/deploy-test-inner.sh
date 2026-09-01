#!/usr/bin/env bash
set -uo pipefail
SRC=/src
WORK=/tmp/w; rm -rf $WORK; mkdir -p $WORK/bin
export PATH=$WORK/bin:$PATH
cp $SRC/tests/fake-docker $WORK/bin/docker
# work on a writable copy of the source
cp -r $SRC /tmp/srccopy; SRC=/tmp/srccopy

PASS=0; FAIL=0
ok(){ printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
no(){ printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
ck(){ if eval "$1"; then ok "$2"; else no "$2"; fi; }

export PREFIX=/tmp/opt ETC=/tmp/etc
"$SRC/install.sh" --check >/tmp/o 2>&1
ck '[[ $? -ne 0 ]]' "--check fails when nothing is installed"
ck 'grep -q "not installed" /tmp/o' "  ...and says so"

# systemctl does not exist in this container; the installer is expected to
# fail at that last step, which is exactly why the version stamp must be
# written only after it succeeds.
printf '#!/bin/sh\nexit 0\n' > "$WORK/bin/systemctl"; chmod +x "$WORK/bin/systemctl"
"$SRC/install.sh" --secrets aws >/tmp/o 2>&1 || { echo "INSTALL FAILED"; cat /tmp/o; }
ck '[[ -f /tmp/opt/.installed ]]' "install writes a version stamp"
ck 'grep -q "^version=" /tmp/opt/.installed' "  ...with a fingerprint"

"$SRC/install.sh" --check >/tmp/o 2>&1
ck '[[ $? -eq 0 ]]' "--check passes immediately after install"
ck 'grep -q "up to date" /tmp/o' "  ...and says up to date"

# simulate `git pull` changing the source but not the deployment
echo "# changed upstream" >> "$SRC/bin/lib/common.sh"
"$SRC/install.sh" --check >/tmp/o 2>&1
ck '[[ $? -ne 0 ]]' "--check detects a stale deployment after a source change"
ck 'grep -q "DEPLOYED COPY IS STALE" /tmp/o' "  ...and says exactly that"
ck 'grep -q "sudo ./install.sh" /tmp/o' "  ...and gives the command to fix it"

# the same divergence must also surface automatically on every real command,
# not just when someone remembers to run --check. check_deployment_freshness
# fires before any config validation, so a nonexistent config is a fine target.
/tmp/opt/bin/s3-backup-discover >/tmp/disc.out 2>/tmp/disc.err
ck 'grep -q "you likely ran .git pull. without redeploying" /tmp/disc.err' \
   "s3-backup-discover warns automatically when the checkout has moved on"
ck '! grep -q "git pull" /tmp/disc.out' \
   "  ...and the warning stays on stderr, out of the config draft on stdout"

CONFIG_FILE=/tmp/nonexistent.env /tmp/opt/bin/s3-backup-status >/tmp/st.out 2>/tmp/st.err
ck 'grep -q "you likely ran .git pull. without redeploying" /tmp/st.err' \
   "s3-backup snapshots/status warns too (via load_config), even before the config is read"

/tmp/opt/bin/s3-backup-setup-aws --config /tmp/nonexistent.env >/tmp/sa.out 2>/tmp/sa.err
ck 'grep -q "you likely ran .git pull. without redeploying" /tmp/sa.err' \
   "s3-backup-setup-aws warns too, despite bypassing load_config"
ck 'grep -q "sudo /tmp/srccopy/install.sh --secrets aws" /tmp/sa.err' \
   "  ...naming the exact redeploy command, including the --secrets flag used originally"

# once redeployed, the warning must go away
"$SRC/install.sh" --secrets aws >/tmp/o 2>&1
/tmp/opt/bin/s3-backup-discover >/tmp/disc2.out 2>/tmp/disc2.err
ck '! grep -q "git pull" /tmp/disc2.err' \
   "the warning is gone immediately after redeploying"

# and a docs-only change must NOT trigger it: the fingerprint only covers
# bin/docker/aws/systemd, so editing docs must not nag on every command.
echo "# doc change" >> "$SRC/docs/index.md"
/tmp/opt/bin/s3-backup-discover >/tmp/disc3.out 2>/tmp/disc3.err
ck '! grep -q "git pull" /tmp/disc3.err' \
   "a docs-only change does not trigger the staleness warning"

# and detects a hand-edited deployment
"$SRC/install.sh" --secrets aws >/tmp/o 2>&1
echo "# hand edit" >> /tmp/opt/bin/lib/common.sh
"$SRC/install.sh" --check >/tmp/o 2>&1
ck 'grep -q "edited by hand" /tmp/o' "--check notices files edited directly under PREFIX"

# second install should not rebuild the image
"$SRC/install.sh" --secrets aws >/tmp/o 2>&1
ck 'grep -q "unchanged, keeping" /tmp/o' "reinstall skips the image build when the Dockerfile is unchanged"

# the stamp must not appear when the install did not finish
rm -rf /tmp/opt /tmp/etc; rm -f "$WORK/bin/systemctl"
"$SRC/install.sh" --secrets aws >/tmp/o 2>&1 || true
ck '[[ ! -f /tmp/opt/.installed ]]' "no version stamp is left behind by a failed install"

printf '%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
