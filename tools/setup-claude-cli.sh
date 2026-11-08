#!/bin/bash
# Lets the team use YOUR CLAUDE SUBSCRIPTION (instead of an API key) as its outside reviewer, by installing the official Claude Code CLI inside LXC 139.
# The CLI is only ever started with every tool, plugin and MCP server switched off (it can answer text and nothing else) and the sign-in stays in the container.
#
#   1. tools/setup-claude-cli.sh            (copies the CLI program from this host into the container; it does NOT copy your login)
#   2. pct enter 139                         then:   su -s /bin/bash prxycmd -c 'HOME=/var/lib/prxy-command claude'
#      sign in with your Claude account when it asks (it prints a link to open on any device), then type /exit
#   3. Settings > Outside model: switch it on (backend "auto" uses the CLI as long as no ANTHROPIC_API_KEY is set), press "Test it".
# Calls count against your plan's usage limits (a daily cap protects them) and there is no per-token bill. Check Anthropic's current terms for automated use.
set -euo pipefail
SRC=$(readlink -f /root/.local/bin/claude)
[ -x "$SRC" ] || { echo "the claude program was not found on this host"; exit 1; }
pct exec 139 -- mkdir -p /opt/claude
pct push 139 "$SRC" /opt/claude/claude --perms 0755
pct exec 139 -- sh -c 'grep -q "^CLAUDE_CLI=" /etc/prxy-command/env || echo "CLAUDE_CLI=/opt/claude/claude" >> /etc/prxy-command/env; grep -q "^CLAUDE_HOME=" /etc/prxy-command/env || echo "CLAUDE_HOME=/var/lib/prxy-command" >> /etc/prxy-command/env; chown -R prxycmd:prxycmd /var/lib/prxy-command; systemctl restart prxy-command'
echo "Installed. Now do step 2 from the comments at the top of this script (sign in once inside the container)."
