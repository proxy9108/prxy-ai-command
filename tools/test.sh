#!/bin/bash
# Run the smoke tests inside LXC 139 (it has the venv) against a throw-away copy of the source; never touches live data. Usage: tools/test.sh
set -e
S=${TMPDIR:-/tmp}/prxy-command-test.tgz
tar --exclude=__pycache__ --exclude=data -czf "$S" -C /root prxy-command
pct push 139 "$S" /root/pt.tgz
pct exec 139 -- bash -c 'rm -rf /tmp/prxy-test && mkdir /tmp/prxy-test && tar -xzf /root/pt.tgz -C /tmp/prxy-test && rm -f /root/pt.tgz && cd /tmp/prxy-test/prxy-command && timeout 900 /opt/prxy-command-venv/bin/python tests/smoke.py 2>&1 | tail -${TAIL:-25}; rm -rf /tmp/prxy-test'
rm -f "$S"
