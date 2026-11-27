#!/usr/bin/env bash
# Install or update the vault viewer on other hosts over SSH, from this checkout.
# Docs: docs/operations.md#deploy-to-other-hosts
# shellcheck disable=SC2016  # single quotes are deliberate: that text runs on the remote host
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_NAME="artifacts.service"
NEED_MB=250
AA_FILE_DEFAULT="/etc/apparmor.d/artifacts-chrome-headless-shell"

usage() {
  cat <<'USAGE'
usage: deploy.sh [--pdf MODE] [-i FILE] [--dry-run] HOST... [-- INSTALL_OPTIONS...]
       deploy.sh -h | --help

Install or update the vault viewer on each HOST over SSH, from this checkout.
HOST is an ssh destination: an alias, a name or user@host.

  A host that runs artifacts.service is updated: the program is replaced (the
  old one kept as <program>.prev), the unit is left as it is, and the service
  restarts. A host without the unit is installed with install.sh, which needs
  INSTALL_OPTIONS after "--" (at least --bind and a ROOT, as seen on that host).
  Given INSTALL_OPTIONS, an installed host is reconfigured with them too.

  The PDF runtime (about 235 MB, under ~/.local/share/artifacts/pdf-runtime on
  the host) must match the program. It is replaced only when its revision
  differs (artifacts.py --pdf-rev), and before the program changes. When the
  host's kernel restricts unprivileged user namespaces, the AppArmor profile
  for Chromium's sandbox is installed there with sudo -n; if that needs a
  password, the commands are printed and the PDF check is skipped.

  --pdf MODE     auto (default): copy this machine's runtime when it matches the
                 program, otherwise download it on the host
                 ship: always copy this machine's runtime (tar over ssh)
                 fetch: always download it on the host (artifacts.py --install-pdf-runtime)
                 skip: leave the runtime alone, and skip the PDF check
  -i FILE        ssh identity file
  --dry-run      probe each host and print the plan, change nothing
  -h, --help     show this help

After a restart the host is checked: the unit is active, the port answers, and
a test page prints to PDF. If that fails, an update is rolled back to the
previous program and runtime. Exit status: 0 all hosts ok, 1 a host failed,
2 usage error.
USAGE
}

die_usage() { echo "deploy.sh: $1" >&2; usage >&2; exit 2; }

MODE=auto
DRY_RUN=0
HOSTS=()
INSTALL_OPTS=()
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15)
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --image|--image=*) die_usage "--image was removed: PDFs no longer use docker images; use --pdf MODE (auto, ship, fetch or skip)" ;;
    --pdf)
      [ "$#" -ge 2 ] || die_usage "--pdf needs a value"
      case "$2" in
        auto|ship|fetch|skip) MODE="$2" ;;
        build) die_usage "--pdf build was removed: PDFs no longer use docker; use --pdf fetch" ;;
        *) die_usage "--pdf must be auto, ship, fetch or skip, got: $2" ;;
      esac
      shift 2 ;;
    -i)
      [ "$#" -ge 2 ] || die_usage "-i needs a file"
      [ -r "$2" ] || die_usage "cannot read identity file: $2"
      SSH+=(-i "$2"); shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --) shift; INSTALL_OPTS=("$@"); break ;;
    -*) die_usage "unknown option: $1" ;;
    *) HOSTS+=("$1"); shift ;;
  esac
done
[ "${#HOSTS[@]}" -gt 0 ] || die_usage "at least one HOST is required"
if [ "${#INSTALL_OPTS[@]}" -gt 0 ] && [ "${#HOSTS[@]}" -gt 1 ]; then
  die_usage "INSTALL_OPTIONS name one host's address and folders: deploy one host at a time with them"
fi
for f in artifacts.py install.sh "systemd/$UNIT_NAME"; do
  [ -f "$HERE/$f" ] || { echo "deploy.sh: $HERE/$f missing" >&2; exit 1; }
done

# One SSH connection per host, reused by every step.
CTL="$(mktemp -d)"
trap 'rm -rf "$CTL"' EXIT
SSH+=(-o ControlMaster=auto -o "ControlPath=$CTL/%C" -o ControlPersist=120)

REV="$(python3 "$HERE/artifacts.py" --pdf-rev)"
LOCAL_RT="${XDG_DATA_HOME:-$HOME/.local/share}/artifacts/pdf-runtime"
LOCAL_REV="$(cat "$LOCAL_RT/$REV/REV" 2>/dev/null || true)"

# The runtime dir ends up in the AppArmor profile and in commands: artifacts.py
# refuses whitespace and glob characters in it, so refuse it here, before anything runs.
runtime_ok() { [[ "$1" =~ ^/[A-Za-z0-9_@%+=:,./~-]*$ ]]; }
# profile_for <runtime dir>: the AppArmor profile for exactly that directory.
profile_for() { python3 "$HERE/artifacts.py" --print-apparmor-profile --pdf-runtime "$1"; }

# remote <host> <script>: run a bash script on the host. The script travels
# base64-encoded in the command, so the remote login shell only sees a fixed
# command, and values go into the script as quoted assignments. remote_in
# passes this program's stdin through (for data), remote closes it.
remote_cmd() { printf 'bash -c "$(echo %s | base64 -d)"' "$(printf '%s' "$1" | base64 | tr -d '\n')"; }
remote_in() { "${SSH[@]}" "$1" "$(remote_cmd "$2")"; }
remote() { remote_in "$@" </dev/null; }
assign() { printf '%s=%q\n' "$@"; }

# Read the installed unit: the program, bind address, port and PDF runtime
# directory. Shared by the probe and the check. Prints key=value lines.
read -r -d '' UNIT_INFO <<'EOF' || true
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/artifacts.service"
if [ -f "$UNIT" ]; then
  echo "unit=yes"
  python3 - "$UNIT" <<'PY'
import os, shlex, sys
line = next(l for l in open(sys.argv[1]) if l.startswith("ExecStart="))
argv = shlex.split(line[len("ExecStart="):].replace("%%", "%").replace("$$", "$"))
def opt(name, default):
    return argv[argv.index(name) + 1] if name in argv[:-1] else default
data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
print(f"bin={argv[1]}")
print(f"bind={opt('--bind', '127.0.0.1')}")
print(f"port={opt('--port', '7777')}")
print(f"runtime={opt('--pdf-runtime', data + '/artifacts/pdf-runtime')}")
PY
else
  echo "unit=no"
  echo "runtime=${XDG_DATA_HOME:-$HOME/.local/share}/artifacts/pdf-runtime"
fi
EOF

# Remote scripts. Each expects the assignments named in its first comment.
# Probe, part two (RT REV PROFILE): the runtime revision and the AppArmor state.
read -r -d '' PROBE_PDF <<'EOF' || true
r="$(cat "$RT/$REV/REV" 2>/dev/null || true)"
echo "rev=$r"
echo "restrict=$(cat "${ARTIFACTS_APPARMOR_RESTRICT_FILE:-/proc/sys/kernel/apparmor_restrict_unprivileged_userns}" 2>/dev/null || echo 0)"
if [ "$(cat "${ARTIFACTS_APPARMOR_PROFILE_FILE:-/etc/apparmor.d/artifacts-chrome-headless-shell}" 2>/dev/null || true)" = "$PROFILE" ]; then
  echo "aa=ok"
else
  echo "aa=missing"
fi
EOF

# Keep the old runtime revisions until the new program passes its check (RT):
# hard links, so no extra space. The install step may delete them from RT.
read -r -d '' PDF_STASH <<'EOF' || true
rm -rf "$RT.keep"
[ ! -d "$RT" ] || cp -al "$RT" "$RT.keep"
EOF

# Receive a runtime tarball on stdin (RT REV SHIP_ID): extract to a temporary
# name, check the tree against the manifest that is the stream's first member,
# write REV (the stream does not carry it), then rename. A half-copied runtime
# never has the final name, and REV exists only on a verified tree.
read -r -d '' PDF_RECEIVE <<'EOF' || true
set -eo pipefail
mkdir -p "$RT"
T="$RT/.tmp-ship-$SHIP_ID"
rm -rf "$T"
mkdir "$T"
trap 'rm -rf "$T"' EXIT
gunzip | tar -x -C "$T"
[ -f "$T/MANIFEST" ] && [ -d "$T/$REV" ] || { echo "the runtime stream has no manifest or no tree" >&2; exit 1; }
(cd "$T/$REV" && find . \( -type f -o -type l \) -printf '%y %m %s %P\n' | LC_ALL=C sort) | cmp -s - "$T/MANIFEST" \
  || { echo "the copied runtime does not match its manifest (incomplete stream?)" >&2; exit 1; }
printf '%s' "$REV" > "$T/$REV/REV"
[ ! -e "$RT/$REV" ] || mv "$RT/$REV" "$T/replaced"
mv "$T/$REV" "$RT/$REV"
EOF

# After a passing check (RT REV SHIP_ID): drop the stash, this deploy's own
# temporary directory and the other revisions. Only names that are exactly 12
# lowercase hex characters are revisions; anything else in the dir is not ours,
# and neither are artifacts.py's .tmp-<rev>-<pid> directories. artifacts.py's
# lock (<runtime dir>/.lock) is held so no install runs while revisions go.
read -r -d '' PDF_FINISH <<'EOF' || true
rm -rf "$RT.keep" "$RT/.tmp-ship-$SHIP_ID"
if command -v flock >/dev/null 2>&1; then
  exec 9>>"$RT/.lock"
  flock -w 60 9 || { echo "could not take $RT/.lock" >&2; exit 1; }
fi
for d in "$RT"/*; do
  n="${d##*/}"
  [ "$n" != "$REV" ] || continue
  [[ "$n" =~ ^[0-9a-f]{12}$ ]] || continue
  [ -d "$d" ] && [ ! -L "$d" ] || continue
  rm -rf "$d"
done
EOF

# Undo the runtime step (RT REV [SHIP_ID]): the previous revisions come back from the stash.
read -r -d '' PDF_RESTORE <<'EOF' || true
rm -rf "$RT/$REV"
[ -z "${SHIP_ID:-}" ] || rm -rf "$RT/.tmp-ship-$SHIP_ID"
if [ -d "$RT.keep" ]; then
  for d in "$RT.keep"/*; do
    [ -e "$d" ] || continue
    [ -e "$RT/$(basename "$d")" ] || mv "$d" "$RT/"
  done
fi
rm -rf "$RT.keep"
EOF

# Install the AppArmor profile with sudo -n (PROFILE).
read -r -d '' AA_INSTALL <<'EOF' || true
set -e
S="${ARTIFACTS_SUDO:-sudo}"
P="${ARTIFACTS_APPARMOR_PARSER:-apparmor_parser}"
F="${ARTIFACTS_APPARMOR_PROFILE_FILE:-/etc/apparmor.d/artifacts-chrome-headless-shell}"
printf '%s\n' "$PROFILE" | "$S" -n tee "$F" >/dev/null
"$S" -n "$P" -r "$F"
EOF

# Probe a host: its unit (if any), python3, free space, the runtime revision and AppArmor.
probe_host() {
  local host="$1" out rt
  out="$(remote "$host" "$UNIT_INFO
command -v python3 >/dev/null 2>&1 && echo python=yes || echo python=no
echo \"free_mb=\$(df -Pm \"\$HOME\" | awk 'NR == 2 { print \$4 }')\"")" || return 1
  rt="$(sed -n 's/^runtime=//p' <<<"$out" | head -n 1)"
  printf '%s\n' "$out"
  if ! runtime_ok "$rt"; then echo "bad_runtime=yes"; return 0; fi
  remote "$host" "$(assign RT "$rt" REV "$REV" PROFILE "$(profile_for "$rt")")"$'\n'"$PROBE_PDF"
}

# get <key> <probe output>
get() { sed -n "s/^$1=//p" <<<"$2" | head -n 1; }

# Check a host after the restart: unit active, port answering, a PDF printed.
check_host() {
  local host="$1" pdf="$2"
  remote "$host" "{ $UNIT_INFO
} >/dev/null
$(assign PDF "$pdf")"'
eval "$(python3 - "$UNIT" <<'"'"'PY'"'"'
import os, shlex, sys
line = next(l for l in open(sys.argv[1]) if l.startswith("ExecStart="))
argv = shlex.split(line[len("ExecStart="):].replace("%%", "%").replace("$$", "$"))
def opt(name, default):
    return argv[argv.index(name) + 1] if name in argv[:-1] else default
data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
for k, v in (("BIN", argv[1]), ("BIND", opt("--bind", "127.0.0.1")), ("PORT", opt("--port", "7777")),
             ("RT", opt("--pdf-runtime", data + "/artifacts/pdf-runtime"))):
    print(f"{k}={shlex.quote(v)}")
PY
)"
systemctl --user is-active --quiet artifacts.service || { echo "artifacts.service is not active"; exit 1; }
code=000
for _ in 1 2 3 4 5 6 7 8 9 10; do
  code="$(curl -s -o /dev/null -m 2 -w "%{http_code}" -H "Host: $BIND:$PORT" "http://$BIND:$PORT/" 2>/dev/null || true)"
  [ -n "$code" ] && [ "$code" != 000 ] && break
  sleep 1
done
case "$code" in
  200|403) echo "port $BIND:$PORT answers (HTTP $code)" ;;
  *) echo "port $BIND:$PORT does not answer (HTTP ${code:-none})"; exit 1 ;;
esac
[ "$PDF" = 1 ] || exit 0
if out="$(python3 "$BIN" --check-pdf-runtime --pdf-runtime "$RT" 2>&1)"; then
  echo "PDF runtime: $out"
else
  echo "test PDF failed: $out"
  exit 1
fi
'
}

FAILED=()
OK=()
deploy_host() {
  local host="$1" info unit py bin rt rev free restrict aa plan aaplan action changed="" swapped=0 aa_manual=0
  echo "== $host"
  if ! info="$(probe_host "$host")"; then
    echo "   cannot reach $host over ssh"; return 1
  fi
  unit="$(get unit "$info")"; py="$(get python "$info")"
  bin="$(get bin "$info")"; rt="$(get runtime "$info")"; rev="$(get rev "$info")"; free="$(get free_mb "$info")"
  restrict="$(get restrict "$info")"; aa="$(get aa "$info")"
  if [ "$(get bad_runtime "$info")" = yes ]; then
    echo "   the PDF runtime directory on $host must be an absolute path without spaces or special"
    echo "   characters (artifacts.py refuses others), got: $rt"
    return 1
  fi
  [ "$py" = yes ] || { echo "   python3 is missing on $host"; return 1; }
  if [ "${#INSTALL_OPTS[@]}" -gt 0 ]; then
    action=install
  elif [ "$unit" = yes ]; then
    action=update
  else
    echo "   the viewer is not installed on $host: pass install.sh options after --,"
    echo "   e.g. deploy.sh $host -- --bind <tailnet IP> --tailnet-user <login> ~/obsidian"
    return 1
  fi

  if [ "$MODE" = skip ]; then plan=keep
  elif [ "$rev" = "$REV" ] && [ "$MODE" = auto ]; then plan=current
  elif [ "$MODE" = ship ] || { [ "$MODE" = auto ] && [ "$LOCAL_REV" = "$REV" ]; }; then plan=ship
  else plan=fetch
  fi
  if [ "$plan" = ship ] && [ "$LOCAL_REV" != "$REV" ]; then
    echo "   --pdf ship: the PDF runtime here ($LOCAL_RT/$REV) is not installed;"
    echo "   install it first: python3 $HERE/artifacts.py --install-pdf-runtime"
    return 1
  fi
  if [ "$plan" = ship ] || [ "$plan" = fetch ]; then
    if [[ "$free" =~ ^[0-9]+$ ]] && [ "$free" -lt "$NEED_MB" ]; then
      echo "   not enough free space on $host: the PDF runtime needs about $NEED_MB MB, $free MB free"; return 1
    fi
  fi
  if [ "$plan" = keep ] || [ "$restrict" != 1 ]; then aaplan=none
  elif [ "$aa" = ok ]; then aaplan=present
  else aaplan=install
  fi

  echo "   viewer: ${action} (unit: ${unit}${bin:+, program $bin})"
  case "$plan" in
    keep) echo "   pdf:    left alone (--pdf skip; revision ${rev:-none}, program $REV)" ;;
    current) echo "   pdf:    runtime $REV is current in $rt" ;;
    ship) echo "   pdf:    copy the PDF runtime from here to $rt (revision ${rev:-none} -> $REV, ${free:-?} MB free)" ;;
    fetch) echo "   pdf:    download the PDF runtime on the host into $rt (revision ${rev:-none} -> $REV, ${free:-?} MB free)" ;;
  esac
  case "$aaplan" in
    install) echo "   apparmor: the host restricts user namespaces: install the profile with sudo -n" ;;
    present) echo "   apparmor: the profile is installed" ;;
  esac
  if [ "$DRY_RUN" -eq 1 ]; then echo "   dry run: nothing changed"; return 0; fi

  # The main loop removes this directory whatever happens.
  TMP_REMOTE="$(remote "$host" 'mktemp -d "${TMPDIR:-/tmp}/vault-viewer.XXXXXX"')" \
    || { echo "   cannot create a temporary directory on $host"; return 1; }
  local tmp="$TMP_REMOTE"
  tar -C "$HERE" -czf - artifacts.py install.sh systemd | remote_in "$host" "$(assign D "$tmp")"$'\ntar -xzf - -C "$D"' \
    || { echo "   copying the files failed"; return 1; }

  # 1. The PDF runtime, before the program: if this fails, nothing has changed.
  #    The old revisions stay in <runtime>.keep until the check passes, for a rollback.
  if [ "$plan" = ship ] || [ "$plan" = fetch ]; then
    local ok=1
    remote "$host" "$(assign RT "$rt")"$'\n'"$PDF_STASH" || ok=0
    if [ "$ok" -eq 1 ] && [ "$plan" = ship ]; then
      echo "   copying the PDF runtime..."
      # The manifest (sorted type, mode, size and path of every file) comes first and the
      # revision's REV file is not sent: the host verifies the tree, then writes REV.
      (cd "$LOCAL_RT/$REV" && find . \( -type f -o -type l \) ! -path ./REV -printf '%y %m %s %P\n' | LC_ALL=C sort) > "$CTL/MANIFEST"
      tar -c -C "$CTL" MANIFEST -C "$LOCAL_RT" --anchored --exclude="$REV/REV" "$REV" | gzip -1 \
        | remote_in "$host" "$(assign RT "$rt" REV "$REV" SHIP_ID "$$")"$'\n'"$PDF_RECEIVE" || ok=0
    elif [ "$ok" -eq 1 ]; then
      echo "   downloading the PDF runtime on the host (about 180 MB)..."
      remote "$host" "$(assign D "$tmp" RT "$rt")"$'\npython3 "$D/artifacts.py" --install-pdf-runtime --pdf-runtime "$RT"' >/dev/null 2>&1 || ok=0
    fi
    if [ "$ok" -eq 0 ]; then
      remote "$host" "$(assign RT "$rt" REV "$REV" SHIP_ID "$$")"$'\n'"$PDF_RESTORE" || true
      echo "   the PDF runtime step failed; nothing on $host was changed"; return 1
    fi
    swapped=1
  fi

  # 1b. The AppArmor profile (sudo -n). Without passwordless sudo, print the commands
  #     and carry on: the viewer works, the PDF button does not until it is installed.
  if [ "$aaplan" = install ]; then
    if remote "$host" "$(assign PROFILE "$(profile_for "$rt")")"$'\n'"$AA_INSTALL" >/dev/null 2>&1; then
      echo "   apparmor profile installed"
    else
      aa_manual=1
      echo "   could not install the AppArmor profile with sudo -n (a password is needed?)."
      echo "   PDF export is NOT working on $host until you run, there:"
      echo "     python3 $([ -n "$bin" ] && printf '%q' "$bin" || printf '%s' '$HOME/.local/bin/artifacts') --print-apparmor-profile --pdf-runtime $(printf '%q' "$rt") | sudo tee $AA_FILE_DEFAULT >/dev/null"
      echo "     sudo apparmor_parser -r $AA_FILE_DEFAULT"
    fi
  fi

  # 2. The program.
  if [ "$action" = update ]; then
    # An identical program is left alone, so a re-run keeps the real .prev.
    changed="$(remote "$host" "$(assign D "$tmp" BIN "$bin")"'
set -e
if cmp -s "$D/artifacts.py" "$BIN"; then
  echo same
else
  cp -p "$BIN" "$BIN.prev"
  install -m 0755 "$D/artifacts.py" "$BIN"
  echo replaced
fi')" || { echo "   replacing the program failed: check $bin by hand"; rollback "$host" "$bin" "$rt" 0 "$swapped"; return 1; }
    if [ "$changed" = replaced ]; then
      remote "$host" 'systemctl --user restart artifacts.service' \
        || { echo "   restarting artifacts.service failed"; rollback "$host" "$bin" "$rt" 1 "$swapped"; return 1; }
    fi
    case "$changed" in
      replaced) echo "   program replaced (previous: $bin.prev), service restarted" ;;
      *) echo "   program unchanged" ;;
    esac
  else
    local opts
    [ "$aa_manual" -eq 0 ] || INSTALL_OPTS+=(--no-apparmor)
    opts="$(printf '%q ' "${INSTALL_OPTS[@]}")"
    remote "$host" "$(assign D "$tmp")"$'\n'"bash \"\$D/install.sh\" $opts" | sed 's/^/   | /' \
      || { echo "   install.sh failed on $host"; return 1; }
  fi

  # 3. The check, and a rollback of an update if it fails.
  local pdf=1
  if [ "$plan" = keep ] || [ "$aa_manual" -eq 1 ]; then pdf=0; fi
  if ! check_host "$host" "$pdf" | sed 's/^/   check: /'; then
    if [ "$action" = update ]; then
      rollback "$host" "$bin" "$rt" "$([ "$changed" = replaced ] && echo 1 || echo 0)" "$swapped"
    fi
    return 1
  fi
  if [ "$swapped" -eq 1 ]; then
    remote "$host" "$(assign RT "$rt" REV "$REV" SHIP_ID "$$")"$'\n'"$PDF_FINISH" && echo "   old PDF runtime revisions removed"
  fi
  [ "$aa_manual" -eq 0 ] || echo "   PDF export on $host: not working until the AppArmor profile is installed (see above)"
  return 0
}

# rollback <host> <bin> <runtime dir> <1 if the program was replaced> <1 if the runtime was replaced>
rollback() {
  local what=""
  [ "$4" = 1 ] && what="program"
  [ "$5" != 1 ] || what="${what:+$what and }PDF runtime"
  echo "   rolling back the ${what:-service (nothing was replaced)}..."
  remote "$1" "$(assign BIN "$2" RT "$3" REV "$REV" SHIP_ID "$$" PROGRAM "$4" RUNTIME "$5")"'
[ "$PROGRAM" != 1 ] || cp -p "$BIN.prev" "$BIN"
if [ "$RUNTIME" = 1 ]; then
'"$PDF_RESTORE"'
fi
systemctl --user restart artifacts.service' \
    && echo "   rolled back; see journalctl --user -u $UNIT_NAME on the host" \
    || echo "   ROLLBACK FAILED: restore $2.prev and the runtime in $3.keep by hand"
}

if [ -n "$LOCAL_REV" ]; then here="installed"; else here="not installed"; fi
echo "program revision $REV; PDF runtime here: $here"
for h in "${HOSTS[@]}"; do
  TMP_REMOTE=""
  if deploy_host "$h"; then OK+=("$h"); else FAILED+=("$h"); fi
  [ -z "$TMP_REMOTE" ] || remote "$h" "$(assign D "$TMP_REMOTE")"$'\nrm -rf "$D"' || true
done
echo
echo "ok: ${OK[*]:-none}"
echo "failed: ${FAILED[*]:-none}"
[ "${#FAILED[@]}" -eq 0 ]
