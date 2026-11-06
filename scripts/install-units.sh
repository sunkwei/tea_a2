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

# a2_wechat@ is the template unit: one instance per WeChat account
# (a2_wechat@alice.service). a2_wechat_pair is the pairing page.
for unit in a2 a2_task a2_wechat 'a2_wechat@' a2_wechat_mul a2_wechat_pair a2_telegram a2_fleet a2_fleet_collect a2_watchdog a2_router; do
    src="scripts/$unit.service"
    [ -f "$src" ] || continue
    out="$dest/$unit.service"
    sed -e "s|%h/work/git/tea_a2|$esc|g" "$src" > "$out"
    echo "wrote $out"
done

# The timer is not a .service file, but it needs the same path rewrite.
if [ -f "scripts/a2_fleet_collect.timer" ]; then
    out="$dest/a2_fleet_collect.timer"
    sed -e "s|%h/work/git/tea_a2|$esc|g" scripts/a2_fleet_collect.timer > "$out"
    echo "wrote $out"
fi

# The trigger rules: the chat front ends go through the trigger entry (the
# units carry --triggers), and without a rules file every message would ask
# the model once whether it is work. The template already holds the chat
# sections (all accepted). An existing file is the operator's own - never
# touched.
rules="$HOME/.tea_agent/triggers.conf"
if [ ! -f "$rules" ]; then
    mkdir -p "$HOME/.tea_agent"
    cp "$repo/sched/triggers.template" "$rules"
    echo "wrote $rules (from the template)"
fi

systemctl --user daemon-reload
echo ""
echo "now: systemctl --user enable --now a2 a2_task a2_wechat a2_telegram a2_watchdog a2_router"
echo "     systemctl --user enable --now a2_wechat_pair          # the pairing page"
echo "     systemctl --user enable --now a2_wechat_mul           # every account in one process"
echo "     systemctl --user start a2_wechat@alice               # one account"
echo "     systemctl --user enable --now a2_fleet a2_fleet_collect.timer   # the fleet"
