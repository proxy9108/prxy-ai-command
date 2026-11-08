#!/bin/bash
# RUN THIS YOURSELF on the Proxmox host. Installs prxy-runner: the service that carries out MISSIONS you approved in Prxy-AI Command.
#
#   tools/install-runner.sh --targets 110,111,...      the containers missions may work in (VMs are never included)
#
# What it does: copies the runner to /usr/local/sbin, creates a random shared secret (never printed) in /etc/prxy-runner.env (mode 600),
# installs and starts prxy-runner.service (listens on 10.20.40.224:8330, accepts only 10.20.40.139 and only signed missions),
# and gives Prxy-AI Command (LXC 139) the runner's address and secret, then restarts it.
# Kill switch:  touch /etc/prxy-runner.disabled      (missions stop before their next step; remove the file to allow again)
# Uninstall:    systemctl disable --now prxy-runner; rm /etc/systemd/system/prxy-runner.service /usr/local/sbin/prxy-runner /etc/prxy-runner.env
#               pct exec 139 -- sed -i '/^RUNNER_/d' /etc/prxy-command/env && pct exec 139 -- systemctl restart prxy-command
set -euo pipefail
TARGETS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --targets) TARGETS="$2"; shift 2;;
    *) echo "unknown option $1"; exit 2;;
  esac
done
[ -n "$TARGETS" ] || { echo "give --targets 110,111,... (the containers missions may work in)"; exit 2; }
echo "$TARGETS" | grep -Eq '^[0-9]+(,[0-9]+)*$' || { echo "--targets must be numbers separated by commas"; exit 2; }
for id in ${TARGETS//,/ }; do pct status "$id" >/dev/null 2>&1 || { echo "container $id does not exist (VMs are not allowed)"; exit 2; }; done
HERE="$(cd "$(dirname "$0")" && pwd)"
install -m 0755 "$HERE/runner/prxy-runner.py" /usr/local/sbin/prxy-runner
SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
umask 077
cat > /etc/prxy-runner.env <<EOF
RUNNER_SECRET=$SECRET
RUNNER_BIND=10.20.40.224
RUNNER_PORT=8330
RUNNER_ALLOW=10.20.40.139
RUNNER_TARGETS=$TARGETS
EOF
chmod 600 /etc/prxy-runner.env
cat > /etc/systemd/system/prxy-runner.service <<'EOF'
[Unit]
Description=prxy-runner: carries out missions approved in Prxy-AI Command
After=network-online.target pve-cluster.service

[Service]
EnvironmentFile=/etc/prxy-runner.env
ExecStart=/usr/bin/python3 /usr/local/sbin/prxy-runner
Restart=on-failure
RestartSec=5
StateDirectory=prxy-runner

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now prxy-runner
printf 'RUNNER_URL=http://10.20.40.224:8330\nRUNNER_SECRET=%s\n' "$SECRET" | pct exec 139 -- sh -c 'sed -i "/^RUNNER_/d" /etc/prxy-command/env && cat >> /etc/prxy-command/env && systemctl restart prxy-command'
unset SECRET
sleep 2
systemctl is-active prxy-runner >/dev/null && echo "Done. prxy-runner is running; missions may work on the host and containers: $TARGETS." || { echo "prxy-runner did not start: journalctl -u prxy-runner"; exit 1; }
echo "Next: open Missions in Prxy-AI Command."
