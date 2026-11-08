#!/usr/bin/env bash
# Install or update the vault viewer on other hosts over SSH, from this checkout.
# Docs: docs/operations.md#deploy-to-other-hosts
# shellcheck disable=SC2016  # single quotes are deliberate: that text runs on the remote host
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_IMAGE="artifacts-pdf:local"
UNIT_NAME="artifacts.service"

usage() {
  cat <<'USAGE'
usage: deploy.sh [--image MODE] [-i FILE] [--dry-run] HOST... [-- INSTALL_OPTIONS...]
       deploy.sh -h | --help

Install or update the vault viewer on each HOST over SSH, from this checkout.
HOST is an ssh destination: an alias, a name or user@host.

  A host that runs artifacts.service is updated: the program is replaced (the
  old one kept as <program>.prev), the unit is left as it is, and the service
  restarts. A host without the unit is installed with install.sh, which needs
  INSTALL_OPTIONS after "--" (at least --bind and a ROOT, as seen on that host).
  Given INSTALL_OPTIONS, an installed host is reconfigured with them too.

  The PDF image must match the program. It is replaced only when its revision
  differs (artifacts.py --pdf-rev), and before the program changes.

  --image MODE   auto (default): copy this machine's image when it matches the
                 program, otherwise build on the host
                 ship: always copy this machine's image (docker save | docker load)
                 build: always build on the host (artifacts.py --build-pdf-image)
                 skip: leave the image alone, and skip the PDF check
  -i FILE        ssh identity file
  --dry-run      probe each host and print the plan, change nothing
  -h, --help     show this help

After a restart the host is checked: the unit is active, the port answers, and
a test page prints to PDF. If that fails, an update is rolled back to the
previous program and image. Exit status: 0 all hosts ok, 1 a host failed,
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
    --image)
      [ "$#" -ge 2 ] || die_usage "--image needs a value"
      case "$2" in auto|ship|build|skip) MODE="$2" ;; *) die_usage "--image must be auto, ship, build or skip, got: $2" ;; esac
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
LOCAL_REV="$(docker image inspect -f '{{ index .Config.Labels "vault-viewer.pdf-rev" }}' "$LOCAL_IMAGE" 2>/dev/null || true)"
[ "$LOCAL_REV" != "<no value>" ] || LOCAL_REV=""

# remote <host> <script>: run a bash script on the host. The script travels
# base64-encoded in the command, so the remote login shell only sees a fixed
# command, and values go into the script as quoted assignments. remote_in
# passes this program's stdin through (for data), remote closes it.
remote_cmd() { printf 'bash -c "$(echo %s | base64 -d)"' "$(printf '%s' "$1" | base64 | tr -d '\n')"; }
remote_in() { "${SSH[@]}" "$1" "$(remote_cmd "$2")"; }
remote() { remote_in "$@" </dev/null; }
assign() { printf '%s=%q\n' "$@"; }

# Read the installed unit: the program, bind address, port and PDF image.
# Shared by the probe and the check. Prints key=value lines.
read -r -d '' UNIT_INFO <<'EOF' || true
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/artifacts.service"
if [ -f "$UNIT" ]; then
  echo "unit=yes"
  python3 - "$UNIT" <<'PY'
import shlex, sys
line = next(l for l in open(sys.argv[1]) if l.startswith("ExecStart="))
argv = shlex.split(line[len("ExecStart="):].replace("%%", "%").replace("$$", "$"))
def opt(name, default):
    return argv[argv.index(name) + 1] if name in argv[:-1] else default
print(f"bin={argv[1]}")
print(f"bind={opt('--bind', '127.0.0.1')}")
print(f"port={opt('--port', '7777')}")
print(f"image={opt('--pdf-image', 'artifacts-pdf:local')}")
PY
else
  echo "unit=no"
  echo "image=artifacts-pdf:local"
fi
EOF

# Probe a host: its unit (if any), python3, docker, free space, and the image revision.
probe_host() {
  local host="$1" out image rev
  out="$(remote "$host" "$UNIT_INFO
command -v python3 >/dev/null 2>&1 && echo python=yes || echo python=no
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then echo docker=yes; else echo docker=no; fi
echo \"free_mb=\$(df -Pm \"\$HOME\" | awk 'NR == 2 { print \$4 }')\"")" || return 1
  image="$(sed -n 's/^image=//p' <<<"$out")"
  rev="$(remote "$host" "$(assign IMAGE "$image")
docker image inspect -f '{{ index .Config.Labels \"vault-viewer.pdf-rev\" }}' \"\$IMAGE\" 2>/dev/null || true")"
  [ "$rev" != "<no value>" ] || rev=""
  printf '%s\nrev=%s\n' "$out" "$rev"
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
import shlex, sys
line = next(l for l in open(sys.argv[1]) if l.startswith("ExecStart="))
argv = shlex.split(line[len("ExecStart="):].replace("%%", "%").replace("$$", "$"))
def opt(name, default):
    return argv[argv.index(name) + 1] if name in argv[:-1] else default
for k, v in (("BIN", argv[1]), ("BIND", opt("--bind", "127.0.0.1")), ("PORT", opt("--port", "7777"))):
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
python3 - "$BIN" <<'"'"'PY'"'"'
import importlib.machinery, importlib.util, sys
loader = importlib.machinery.SourceFileLoader("artifacts_check", sys.argv[1])
spec = importlib.util.spec_from_loader(loader.name, loader)
A = importlib.util.module_from_spec(spec)
loader.exec_module(A)
image = A.Handler.pdf_image
pdf, err = A.render_pdf(A.render_page("deploy check", "<h1>deploy check</h1><p>ok</p>", pdf=True), image)
if err:
    sys.exit(f"test PDF failed: {err}")
print(f"test page printed to PDF ({len(pdf)} bytes)")
PY
'
}

FAILED=()
OK=()
deploy_host() {
  local host="$1" info unit py dock bin image rev free plan action changed="" swapped=0 prev=""
  echo "== $host"
  if ! info="$(probe_host "$host")"; then
    echo "   cannot reach $host over ssh"; return 1
  fi
  unit="$(get unit "$info")"; py="$(get python "$info")"; dock="$(get docker "$info")"
  bin="$(get bin "$info")"; image="$(get image "$info")"; rev="$(get rev "$info")"; free="$(get free_mb "$info")"
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
  else plan=build
  fi
  if [ "$plan" = ship ] && [ "$LOCAL_REV" != "$REV" ]; then
    echo "   --image ship: $LOCAL_IMAGE here does not match this program (revision ${LOCAL_REV:-none}, program $REV);"
    echo "   build it first: python3 $HERE/artifacts.py --build-pdf-image"
    return 1
  fi
  if [ "$plan" = ship ] || [ "$plan" = build ]; then
    [ "$dock" = yes ] || { echo "   docker is missing or not usable on $host (needed for the PDF image)"; return 1; }
  fi

  echo "   viewer: ${action} (unit: ${unit}${bin:+, program $bin})"
  case "$plan" in
    keep) echo "   image:  left alone (--image skip; revision ${rev:-none}, program $REV)" ;;
    current) echo "   image:  $image is current (revision $REV)" ;;
    ship) echo "   image:  copy $LOCAL_IMAGE from here to $image (revision ${rev:-none} -> $REV, ${free:-?} MB free)" ;;
    build) echo "   image:  build $image on the host (revision ${rev:-none} -> $REV, ${free:-?} MB free)" ;;
  esac
  if [ "$DRY_RUN" -eq 1 ]; then echo "   dry run: nothing changed"; return 0; fi

  # The main loop removes this directory whatever happens.
  TMP_REMOTE="$(remote "$host" 'mktemp -d "${TMPDIR:-/tmp}/vault-viewer.XXXXXX"')" \
    || { echo "   cannot create a temporary directory on $host"; return 1; }
  local tmp="$TMP_REMOTE"
  tar -C "$HERE" -czf - artifacts.py install.sh systemd | remote_in "$host" "$(assign D "$tmp")"$'\ntar -xzf - -C "$D"' \
    || { echo "   copying the files failed"; return 1; }

  # 1. The image, before the program: if this fails, nothing has changed. The
  #    old image keeps a <image>-prev tag until the check passes, for a rollback.
  if [ "$plan" = ship ] || [ "$plan" = build ]; then
    if [ "$(remote "$host" "$(assign IMAGE "$image" PREV "${image}-prev")"$'\nif docker image inspect "$IMAGE" >/dev/null 2>&1; then docker tag "$IMAGE" "$PREV" && echo tagged; fi')" = tagged ]; then
      prev="${image}-prev"
    fi
    local ok=1
    if [ "$plan" = ship ]; then
      echo "   copying the image..."
      docker save "$LOCAL_IMAGE" | gzip -1 | remote_in "$host" "$(assign IMAGE "$image" LOCAL "$LOCAL_IMAGE")"'
gunzip | docker load >/dev/null && { [ "$IMAGE" = "$LOCAL" ] || docker tag "$LOCAL" "$IMAGE"; }' || ok=0
    else
      echo "   building the image on the host (a few minutes)..."
      remote "$host" "$(assign D "$tmp" IMAGE "$image")"$'\npython3 "$D/artifacts.py" --build-pdf-image --pdf-image "$IMAGE"' >/dev/null 2>&1 || ok=0
    fi
    if [ "$ok" -eq 0 ]; then
      [ -z "$prev" ] || remote "$host" "$(assign PREV "$prev")"$'\ndocker image rm "$PREV" >/dev/null 2>&1' || true
      echo "   the image step failed; nothing on $host was changed"; return 1
    fi
    swapped=1
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
fi')" || { echo "   replacing the program failed: check $bin by hand"; rollback "$host" "$bin" "$image" "$prev" 0; return 1; }
    if [ "$changed" = replaced ] || [ "$swapped" = 1 ]; then
      remote "$host" 'systemctl --user restart artifacts.service' \
        || { echo "   restarting artifacts.service failed"; rollback "$host" "$bin" "$image" "$prev" "$([ "$changed" = replaced ] && echo 1 || echo 0)"; return 1; }
    fi
    case "$changed" in
      replaced) echo "   program replaced (previous: $bin.prev), service restarted" ;;
      *) if [ "$swapped" = 1 ]; then echo "   program unchanged, service restarted for the new image"; else echo "   program unchanged"; fi ;;
    esac
  else
    local opts
    opts="$(printf '%q ' "${INSTALL_OPTS[@]}")"
    remote "$host" "$(assign D "$tmp")"$'\n'"bash \"\$D/install.sh\" $opts" | sed 's/^/   | /' \
      || { echo "   install.sh failed on $host"; return 1; }
  fi

  # 3. The check, and a rollback of an update if it fails.
  local pdf=1
  [ "$plan" = keep ] && pdf=0
  if ! check_host "$host" "$pdf" | sed 's/^/   check: /'; then
    if [ "$action" = update ]; then
      rollback "$host" "$bin" "$image" "$prev" "$([ "$changed" = replaced ] && echo 1 || echo 0)"
    fi
    return 1
  fi
  if [ -n "$prev" ]; then
    remote "$host" "$(assign PREV "$prev")"$'\ndocker image rm "$PREV" >/dev/null 2>&1' && echo "   old image removed"
  fi
  return 0
}

# rollback <host> <bin> <image> <prev image tag or ""> <1 if the program was replaced>
rollback() {
  local what=""
  [ "$5" = 1 ] && what="program"
  [ -z "$4" ] || what="${what:+$what and }image"
  echo "   rolling back the ${what:-service (nothing was replaced)}..."
  remote "$1" "$(assign BIN "$2" IMAGE "$3" PREV "$4" PROGRAM "$5")"'
[ "$PROGRAM" != 1 ] || cp -p "$BIN.prev" "$BIN"
[ -z "$PREV" ] || { docker tag "$PREV" "$IMAGE" && docker image rm "$PREV" >/dev/null; }
systemctl --user restart artifacts.service' \
    && echo "   rolled back; see journalctl --user -u $UNIT_NAME on the host" \
    || echo "   ROLLBACK FAILED: restore $2.prev and $3 by hand"
}

echo "program revision $REV; $LOCAL_IMAGE here: ${LOCAL_REV:-not built}"
for h in "${HOSTS[@]}"; do
  TMP_REMOTE=""
  if deploy_host "$h"; then OK+=("$h"); else FAILED+=("$h"); fi
  [ -z "$TMP_REMOTE" ] || remote "$h" "$(assign D "$TMP_REMOTE")"$'\nrm -rf "$D"' || true
done
echo
echo "ok: ${OK[*]:-none}"
echo "failed: ${FAILED[*]:-none}"
[ "${#FAILED[@]}" -eq 0 ]
