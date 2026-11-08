#!/bin/bash
# Push /root/prxy-command to LXC 139 and restart the service. Usage: ./deploy.sh
set -e
cd /root/prxy-command
if find static command server.py -type f -size 0 ! -name __init__.py | grep -q .; then echo 'REFUSING TO DEPLOY: an empty file exists:'; find static command server.py -type f -size 0 ! -name __init__.py; exit 1; fi
S=${TMPDIR:-/tmp}/prxy-command-deploy.tgz
tar --exclude=__pycache__ --exclude=data --exclude=deploy.sh -czf "$S" -C /root prxy-command
pct push 139 "$S" /root/pc.tgz
pct exec 139 -- bash -c 'rm -rf /opt/prxy-command && tar -xzf /root/pc.tgz -C /opt && chown -R root:root /opt/prxy-command && rm -f /root/pc.tgz && systemctl restart prxy-command && sleep 2 && systemctl is-active prxy-command'
rm -f "$S"
pct exec 139 -- mkdir -p /var/lib/prxy-command/docs
/root/prxy-command/tools/sync-docs.sh >/dev/null
# the runner on this host: update its program when it changed (the secret and the allowed containers in /etc/prxy-runner.env stay as they are)
if [ -f /usr/local/sbin/prxy-runner ] && ! cmp -s tools/runner/prxy-runner.py /usr/local/sbin/prxy-runner; then
  if grep -lsq '"state": "running"' /var/lib/prxy-runner/mission-*.json; then echo "runner NOT updated: a mission is running. Run ./deploy.sh again when it has finished."; exit 0; fi
  python3 -m py_compile tools/runner/prxy-runner.py && install -m 0755 tools/runner/prxy-runner.py /usr/local/sbin/prxy-runner && systemctl restart prxy-runner && sleep 1 && echo "runner updated: $(systemctl is-active prxy-runner)"
fi
