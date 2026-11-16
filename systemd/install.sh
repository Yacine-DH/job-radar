#!/usr/bin/env bash
# Link and enable the job radar's systemd *user* units from this repo. Safe to re-run.
# The unit files hard-code /home/user/job-radar and the Tailscale IP: edit them first on another machine.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
units=(job-radar.service job-radar.timer job-radar-dashboard.service job-radar-worker.service)
for u in "${units[@]}"; do
  link="$HOME/.config/systemd/user/$u"
  [ -L "$link" ] && rm -f "$link"          # replace a link that points at an old location
  systemctl --user link "$here/$u"
done
systemctl --user daemon-reload
systemctl --user enable --now "$here/job-radar.timer" "$here/job-radar-dashboard.service" "$here/job-radar-worker.service"
systemctl --user list-timers job-radar.timer --no-pager
