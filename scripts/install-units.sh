#!/bin/sh
# Install the user units, with the paths of *this* machine.
#
#   scripts/install-units.sh                    # the repo this script lives in
#   scripts/install-units.sh /opt/tea_a2        # a repo somewhere else
#
# The units in scripts/ are templates: they say %h (the user's home) and
# ~/work/git/tea_a2 (where the repo usually is). This script copies them to
# ~/.config/systemd/user/ and replaces the repo path with the real one, so a
# device whose checkout lives elsewhere needs no hand editing.
#
# It does not start anything: after it, `systemctl --user enable --now ...`
# is what actually runs the services.
set -eu

repo=${1:-$(cd "$(dirname "$0")/.." && pwd)}
dest="$HOME/.config/systemd/user"
mkdir -p "$dest"

# sed's replacement must not contain & or the delimiter.
esc=$(printf '%s' "$repo" | sed -e 's/[&|]/\\&/g')

for unit in a2 a2_task a2_wechat a2_telegram; do
    src="scripts/$unit.service"
    [ -f "$src" ] || continue
    out="$dest/$unit.service"
    sed -e "s|%h/work/git/tea_a2|$esc|g" "$src" > "$out"
    echo "wrote $out"
done

systemctl --user daemon-reload
echo ""
echo "now: systemctl --user enable --now a2 a2_task a2_wechat a2_telegram"
