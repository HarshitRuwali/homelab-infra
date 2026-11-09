#!/bin/sh
# One syslog line a minute with what pf, the resolver and the NICs are doing,
# so Grafana can alert on state-table exhaustion and packet floods. The firewall
# has no metrics agent; syslog to Loki is the path it already has.
# Counters are cumulative: query them with rate_counter() or increase().
# PFSTATS_STDOUT=1 prints the line instead of sending it, for testing.
PATH=/sbin:/usr/sbin:/bin:/usr/bin:/usr/local/bin:/usr/local/sbin
si=$(pfctl -si 2>/dev/null)
states=$(echo "$si" | awk '/current entries/ {print $3; exit}')
state_hits=$(echo "$si" | awk '$1=="state-limit" {print $2}')
src_hits=$(echo "$si" | awk '$1=="src-limit" {print $2}')
states_max=$(pfctl -sm 2>/dev/null | awk '$1=="states" {print $4}')
src_nodes=$(pfctl -sS 2>/dev/null | wc -l | tr -d ' ')
banned=$(pfctl -t virusprot -T show 2>/dev/null | wc -l | tr -d ' ')
load1=$(sysctl -n vm.loadavg | tr -d '{}' | awk '{print $1}')
mem_free_mb=$(( $(sysctl -n vm.stats.vm.v_free_count) * $(sysctl -n hw.pagesize) / 1048576 ))
nics=$(netstat -ibn | awk '$1 ~ /^vtnet[0-9]+$/ && $3 ~ /^<Link/ {printf " %s_ipkts=%s %s_opkts=%s %s_ibytes=%s %s_obytes=%s %s_idrop=%s", $1,$5,$1,$9,$1,$8,$1,$11,$1,$7}')
line="states=${states:-0} states_max=${states_max:-0} state_limit_hits=${state_hits:-0} src_limit_hits=${src_hits:-0} src_nodes=${src_nodes:-0} banned=${banned:-0} load1=${load1:-0} mem_free_mb=${mem_free_mb:-0}${nics}"
if [ -n "$PFSTATS_STDOUT" ]; then echo "$line"; else logger -t pfstats "$line"; fi
