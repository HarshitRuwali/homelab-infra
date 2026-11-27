#!/bin/sh
# Start a virtual display, then the Obsidian app with one vault open and its
# command line interface on. Runs as PID 2 under `docker run --init`; when the
# app exits the container exits and the restart policy brings it back.
set -eu

cfg="$HOME/.config/obsidian"
mkdir -p "$cfg" "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

# The app's global settings, rewritten on every start so the container always
# opens exactly this vault: "cli" is the Settings > General > Command line
# interface switch, "updateDisabled" stops the app from fetching updates (the
# container has no network anyway; the image is updated instead).
printf '{"vaults":{"vault":{"path":"%s","ts":0,"open":true}},"updateDisabled":true,"cli":true}\n' \
  "$OBSIDIAN_VAULT" > "$cfg/obsidian.json"

# The vault's own app settings (in a volume, see setup.sh), seeded once: nobody
# can answer a dialog here, so renames update links without asking, and deleted
# files go to the vault's .trash folder rather than a trash inside the container.
vcfg="$OBSIDIAN_VAULT/.obsidian"
if [ -d "$vcfg" ] && [ ! -e "$vcfg/app.json" ]; then
  printf '{"alwaysUpdateLinks":true,"trashOption":"local"}\n' > "$vcfg/app.json"
fi

display="${DISPLAY#:}"
rm -f "/tmp/.X${display}-lock" "/tmp/.X11-unix/X${display}"
Xvfb "$DISPLAY" -screen 0 1280x800x24 -nolisten tcp >/dev/null 2>&1 &
i=0
while [ ! -S "/tmp/.X11-unix/X${display}" ]; do
  i=$((i + 1))
  [ "$i" -le 50 ] || { echo "obsidian-entrypoint: Xvfb did not start" >&2; exit 1; }
  sleep 0.1
done

# Chromium's own sandbox needs user namespaces the container does not grant;
# the container is the sandbox instead (no network, no capabilities, read-only
# root, unprivileged user). See ../docs/obsidian.md, "Security".
exec /opt/obsidian/obsidian --no-sandbox --disable-gpu --disable-dev-shm-usage "$@"
