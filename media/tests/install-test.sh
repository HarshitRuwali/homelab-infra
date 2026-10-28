#!/usr/bin/env bash
#
# install-test.sh - run install.sh against a scratch MEDIA_DIR with fake
# docker, findmnt, ss, sudo, curl, sleep and tailscale commands first on PATH.
# No real Docker, no network and no root are needed, and nothing outside a
# temporary directory is touched. It asserts the properties of the installer
# that are easy to break by editing it:
#
#   - qBittorrent.conf is seeded when missing and never touched when present
#   - the compose file is validated before anything is installed
#   - --no-tailscale-serve removes an existing serve
#   - a MEDIA_DIR that disagrees with the installed .env is refused
#   - the verify step polls the WebUI and checks IPv4 exit addresses
#
#   ./tests/install-test.sh
#
# shellcheck disable=SC2016  # single quotes are deliberate: literal $ in patterns
set -uo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." || exit 1

PASS=0; FAIL=0
ok() { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
no() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }

# install.sh checks /dev/net/tun itself, and a test cannot fake a device node.
if [[ ! -e /dev/net/tun ]]; then
  echo "SKIP: /dev/net/tun is missing on this host, install.sh would stop in preflight"
  exit 0
fi

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
BIN="$T/bin"; mkdir -p "$BIN"
LOG="$T/fake.log"; : > "$LOG"

# --- fakes ------------------------------------------------------------------
fake() { printf '#!/usr/bin/env bash\n%s\n' "$2" > "$BIN/$1"; chmod +x "$BIN/$1"; }

fake docker '
echo "docker $*" >> "$FAKE_LOG"
case "$*" in
  "compose version --short") echo 2.40.0 ;;
  "compose version")         echo "Docker Compose version v2.40.0" ;;
  *" config -q")             [[ -z "${FAKE_CONFIG_FAIL:-}" ]] || { echo "fake: compose invalid" >&2; exit 1; } ;;
  "inspect "*)               echo healthy ;;
  "exec gluetun wget "*)     echo "${FAKE_VPN_IP-198.51.100.7}" ;;
esac
exit 0'
fake findmnt 'exit 0'
fake ss 'exit 0'
fake sleep 'exit 0'
# sudo without privileges: run the command, dropping -o/-g from install.
fake sudo '
if [[ "$1" == install ]]; then
  shift; args=()
  while (($#)); do case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac; done
  exec /usr/bin/install "${args[@]}"
fi
exec "$@"'
fake tailscale '
echo "tailscale $*" >> "$FAKE_LOG"
[[ "$*" == *" off" ]] && { echo "serve config not found" >&2; exit 1; }
exit 0'
# curl: Plex answers; the WebUI refuses FAKE_WEBUI_REFUSALS times, then 200;
# the IPv4 endpoint answers only when -4 is given.
fake curl '
args=" $* "
echo "curl $*" >> "$FAKE_LOG"
if [[ "$args" == *"%{http_code}"* ]]; then
  n=$(cat "$FAKE_STATE/webui" 2>/dev/null || echo 0); echo $((n+1)) > "$FAKE_STATE/webui"
  if (( n < ${FAKE_WEBUI_REFUSALS:-0} )); then printf 000; exit 7; fi
  printf 200; exit 0
fi
if [[ "$args" == *"ipv4.icanhazip.com"* ]]; then
  [[ "$args" == *" -4 "* ]] || exit 6
  [[ -z "${FAKE_HOST_IP_EMPTY:-}" ]] || exit 6
  echo "${FAKE_HOST_IP-203.0.113.5}"; exit 0
fi
exit 0'

# --- helpers ----------------------------------------------------------------
# newdir <name>: a MEDIA_DIR with the settings and a VPN key in place, and a
# media share, all inside the scratch directory.
newdir() {
  local d="$T/$1"
  mkdir -p "$d/secrets" "$T/share/Torrent"
  echo dummy > "$d/secrets/wireguard_private_key"
  sed -e "s#^MEDIA_DIR=.*#MEDIA_DIR=$d#" -e "s#^MEDIA_ROOT=.*#MEDIA_ROOT=$T/share#" .env.example > "$d/.env"
  printf '%s' "$d"
}
# inst <media_dir> <install.sh args...>: output in $out, exit code in $rc.
inst() {
  local d="$1"; shift
  out="$T/out.$RANDOM"
  FAKE_LOG="$LOG" FAKE_STATE="$T/state" PATH="$BIN:$PATH" MEDIA_DIR="$d" \
    ./install.sh "$@" > "$out" 2>&1
  rc=$?
}
reset() { rm -rf "$T/state"; mkdir -p "$T/state"; unset FAKE_CONFIG_FAIL FAKE_VPN_IP FAKE_HOST_IP FAKE_HOST_IP_EMPTY FAKE_WEBUI_REFUSALS; : > "$LOG"; }
has()   { grep -qE -e "$1" "$out"; }
line()  { grep -nE -e "$1" "$out" | head -1 | cut -d: -f1; }
check() { # check <description> <command...>
  local desc="$1"; shift
  if "$@"; then ok "$desc"; else no "$desc"; sed 's/^/        | /' "$out" | head -40; fi
}
nothas() { ! has "$1"; }

SEED_RE='install -m 0640 -o 100000 -g 100000 .*/qBittorrent\.conf .*/config/qbittorrent/qBittorrent/qBittorrent\.conf'

echo "== dry run: the qBittorrent.conf seed step =="
reset; d="$(newdir seed-missing)"; inst "$d"
check "dry run exits 0" test "$rc" -eq 0
check "dry run prints the seed step when qBittorrent.conf is missing" has "$SEED_RE"
check "dry run creates the qBittorrent config directory step" has 'install -d -m 0750 -o 100000 -g 100000 .*/config/qbittorrent/qBittorrent$'
check "dry run does not create the file" test ! -e "$d/config/qbittorrent/qBittorrent/qBittorrent.conf"
check "dry run installs nothing" test ! -e "$d/compose.yaml"

reset; d="$(newdir seed-present)"
mkdir -p "$d/config/qbittorrent/qBittorrent"; echo custom > "$d/config/qbittorrent/qBittorrent/qBittorrent.conf"
inst "$d"
check "dry run does not print the seed step when qBittorrent.conf exists" nothas "$SEED_RE"
check "dry run says the existing qBittorrent.conf is kept" has 'qBittorrent\.conf kept as is'

echo
echo "== validation happens before anything is installed =="
reset; d="$(newdir order)"; inst "$d"
v="$(line 'compose file validates')"; f="$(line 'install -m 0644 .*/compose\.yaml')"
check "validation output appears before the compose.yaml install step (lines ${v:-none} and ${f:-none})" \
  test -n "$v" -a -n "$f" -a "${v:-9999}" -lt "${f:-0}"
reset; d="$(newdir invalid)"; echo "stale" > "$d/compose.yaml"
FAKE_CONFIG_FAIL=1 inst "$d" --apply
check "an invalid compose file stops --apply" test "$rc" -ne 0
check "an invalid compose file does not replace the installed copy" test "$(cat "$d/compose.yaml")" = stale
check "an invalid compose file is reported" has 'compose\.yaml does not validate'

echo
echo "== --no-tailscale-serve =="
reset; d="$(newdir ts-on)"; inst "$d"
check "the default publishes the WebUI" has 'tailscale serve --bg --tcp 8081 tcp://127\.0\.0\.1:8081'
check "the default does not remove a serve" nothas 'serve --tcp 8081 off'
reset; d="$(newdir ts-off)"; inst "$d" --no-tailscale-serve
check "--no-tailscale-serve prints the serve off action" has 'tailscale serve --tcp 8081 off'
check "--no-tailscale-serve does not publish the WebUI" nothas 'serve --bg'
check "dry run does not run tailscale" test -z "$(grep '^tailscale' "$LOG")"

echo
echo "== MEDIA_DIR must match the installed .env =="
reset; d="$(newdir mismatch)"
sed -i "s#^MEDIA_DIR=.*#MEDIA_DIR=/somewhere/else#" "$d/.env"
inst "$d"
check "a different MEDIA_DIR in an existing .env fails" test "$rc" -ne 0
check "the failure names MEDIA_DIR and both values" has "MEDIA_DIR is '$d' but .*says '/somewhere/else'"
check "nothing else runs after the failure" nothas '^directories'
reset; d="$(newdir nounset)"; sed -i '/^MEDIA_DIR=/d' "$d/.env"; inst "$d"
check "an existing .env without a MEDIA_DIR line fails" test "$rc" -ne 0

echo
echo "== --apply against fakes =="
reset; d="$(newdir apply)"
FAKE_WEBUI_REFUSALS=3 inst "$d" --apply --no-tailscale-serve
check "--apply succeeds when the WebUI refuses at first and tailscale serve off fails" test "$rc" -eq 0
check "the compose file was installed" test -f "$d/compose.yaml"
conf="$d/config/qbittorrent/qBittorrent/qBittorrent.conf"
printf '%s\n' '[Preferences]' 'WebUI\Port=8081' 'WebUI\LocalHostAuth=false' > "$T/expected.conf"
check "qBittorrent.conf was seeded with exactly the three lines" cmp -s "$conf" "$T/expected.conf"
check "qBittorrent.conf is mode 0640" test "$(stat -c %a "$conf")" = 640
check "the WebUI poll retried past three refusals" test "$(cat "$T/state/webui" 2>/dev/null || echo 0)" -ge 4
check "a failing tailscale serve off is reported, not fatal" has 'ignored: command failed'
echo custom > "$conf"
reset; inst "$d" --apply
check "a second --apply leaves an existing qBittorrent.conf alone" test "$(cat "$conf")" = custom
check "a second --apply succeeds" test "$rc" -eq 0

reset; d="$(newdir noip)"
FAKE_HOST_IP_EMPTY=1 inst "$d" --apply
check "an unknown host address fails the VPN check" test "$rc" -ne 0
check "the failure says the host's IPv4 address is unknown" has "cannot determine the host's public IPv4"
reset; d="$(newdir sameip)"
FAKE_VPN_IP=203.0.113.5 inst "$d" --apply
check "an exit address equal to the host's fails" test "$rc" -ne 0
reset; d="$(newdir spaces)"
FAKE_VPN_IP='203.0.113.5  ' inst "$d" --apply
check "addresses are compared trimmed" test "$rc" -ne 0

echo
printf '%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
