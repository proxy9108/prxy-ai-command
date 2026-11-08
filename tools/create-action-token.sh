#!/bin/bash
# RUN THIS YOURSELF on the Proxmox host (the team's AI cannot and does not create credentials).
# It creates the ONE credential the executor uses to write to Proxmox, with only the privileges the actions you pick need, and stores its secret in the
# container's locked env file without printing it.
#
#   tools/create-action-token.sh --vmids 110,131 [--allow-backup-job]
#
#   --vmids 110,131     the guests the team may touch (start, shut down, reboot, snapshot, back up now, change a mount point's Backup flag). Required: privileges are
#                       granted per guest, not globally. The token can also write new backup archives to the 'backups' storage (needed for "back up now").
#   --allow-backup-job  also let it edit the backup job's exclude list. Proxmox only allows that with Sys.Modify on / (a broad privilege: it can change many
#                       datacenter settings). Leave it out if you would rather do that one by hand.
# Afterwards: Settings > Actions: switch the executor on and tick the actions you want.
# To revoke everything later:  pveum user token remove prxyact@pve act; pveum user delete prxyact@pve; pveum role delete PrxyAct; pveum role delete PrxyActJobs; pveum role delete PrxyActStore
set -euo pipefail
VMIDS=""; JOB=0
while [ $# -gt 0 ]; do
  case "$1" in
    --vmids) VMIDS="$2"; shift 2;;
    --allow-backup-job) JOB=1; shift;;
    *) echo "unknown option $1"; exit 2;;
  esac
done
[ -n "$VMIDS" ] || { echo "give --vmids 110,131 (the guests the team may touch)"; exit 2; }
echo "$VMIDS" | grep -Eq '^[0-9]+(,[0-9]+)*$' || { echo "--vmids must be numbers separated by commas"; exit 2; }
U="prxyact@pve"; T="act"
PRIVS="VM.Audit VM.PowerMgmt VM.Config.Disk VM.Snapshot VM.Backup"
pveum role add PrxyAct -privs "$PRIVS" 2>/dev/null || pveum role modify PrxyAct -privs "$PRIVS"
pveum role add PrxyActStore -privs "Datastore.AllocateSpace Datastore.Audit" 2>/dev/null || pveum role modify PrxyActStore -privs "Datastore.AllocateSpace Datastore.Audit"
pveum user list --output-format json | grep -q "\"$U\"" || pveum user add "$U" --comment "Prxy-AI Command executor (writes only what the allowed actions need)"
if pveum user token list "$U" --output-format json 2>/dev/null | grep -q "\"$T\""; then pveum user token remove "$U" "$T"; fi
SECRET=$(pveum user token add "$U" "$T" --privsep 1 --comment "Prxy-AI Command executor" --output-format json | python3 -c 'import sys,json;print(json.load(sys.stdin)["value"])')
for id in ${VMIDS//,/ }; do
  pveum acl modify "/vms/$id" --users "$U" --tokens "$U!$T" --roles PrxyAct          # the user and the token both need it: with privilege separation the token gets the overlap
done
pveum acl modify /storage/backups --users "$U" --tokens "$U!$T" --roles PrxyActStore     # "back up now" writes an archive here (and only here)
if [ "$JOB" = 1 ]; then
  pveum role add PrxyActJobs -privs "Sys.Modify" 2>/dev/null || true
  pveum acl modify / --users "$U" --tokens "$U!$T" --roles PrxyActJobs --propagate 0
fi
printf 'PVE_ACT_TOKEN_ID=%s\nPVE_ACT_TOKEN_SECRET=%s\n' "$U!$T" "$SECRET" | pct exec 139 -- sh -c 'sed -i "/^PVE_ACT_TOKEN_/d" /etc/prxy-command/env && cat >> /etc/prxy-command/env && systemctl restart prxy-command'
unset SECRET
echo "Done. Action token $U!$T created for guests: $VMIDS$([ "$JOB" = 1 ] && echo ' + backup job exclude list'). Its secret is only in /etc/prxy-command/env in LXC 139."
echo "Next: Settings > Actions in the dashboard."
