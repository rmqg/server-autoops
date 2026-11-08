#!/bin/sh
# mem-guard: observe-only memory watcher. It never stops, kills or restarts anything.
# - every TREND_EVERY loops: one line to $LOG_DIR/mem-guard.trend
# - when memory is tight: a snapshot of the biggest processes and containers
# - when it stays severe for NEED loops: one warning alert (again only after recovery)
#
# Settings come from the environment (systemd EnvironmentFile / procd env), all optional.
# Dry run (prints instead of alerting):
#   DRY_RUN=1 SNAP_KB=9999999999 ALERT_KB=9999999999 NEED=2 INTERVAL=1 MAX_LOOPS=4 ./mem-guard.sh

LOG_DIR=${LOG_DIR:-/var/log/mem-guard}
ALERT_CMD=${ALERT_CMD:-server-autoops alert}   # receives --service/--fault-code/... arguments
SNAP_KB=${SNAP_KB:-819200}    # snapshot below 800 MiB available, or PSI full avg60 >= SNAP_PSI
SNAP_PSI=${SNAP_PSI:-15}
ALERT_KB=${ALERT_KB:-512000}  # alert below 500 MiB, or PSI >= ALERT_PSI, for NEED loops in a row
ALERT_PSI=${ALERT_PSI:-40}
INTERVAL=${INTERVAL:-15}
NEED=${NEED:-4}
SNAP_EVERY=${SNAP_EVERY:-4}
TREND_EVERY=${TREND_EVERY:-4}
KEEP=${KEEP:-40}
DRY_RUN=${DRY_RUN:-0}
MAX_LOOPS=${MAX_LOOPS:-0}

log() {
	command -v logger >/dev/null 2>&1 && logger -t mem-guard "$*"
	[ "$DRY_RUN" = 1 ] && echo "mem-guard: $*"
}

alert() {
	[ "$DRY_RUN" = 1 ] && { log "(dry-run) alert: $1 / $2"; return 0; }
	# shellcheck disable=SC2086
	$ALERT_CMD --service host --fault-code MEMORY_PRESSURE --level warning \
		--title "$1" --summary "$2" >/dev/null 2>&1 || log "alert command failed"
}

meminfo_kb() {
	while read -r key value _; do
		[ "$key" = "$1:" ] && { echo "$value"; return; }
	done </proc/meminfo
	echo 0
}

# share of the last 60 s in which all tasks stalled on memory (integer %); 0 without PSI
psi_full() {
	[ -r /proc/pressure/memory ] || { echo 0; return; }
	while read -r kind _ a60 _; do
		[ "$kind" = "full" ] && { v=${a60#avg60=}; echo "${v%%.*}"; return; }
	done </proc/pressure/memory
	echo 0
}

log_dir() {
	mkdir -p "$LOG_DIR" 2>/dev/null && [ -w "$LOG_DIR" ] && { echo "$LOG_DIR"; return; }
	echo /tmp
}

snapshot() {
	(
		dir=$(log_dir)
		file="$dir/mem-guard-$(date +%Y%m%dT%H%M%S).log"
		{
			date
			head -6 /proc/meminfo
			grep -E '^Swap(Total|Free)' /proc/meminfo
			cat /proc/swaps 2>/dev/null
			cat /proc/pressure/memory 2>/dev/null
			echo '--- top rss+swap: total_kB rss_kB swap_kB pid comm cgroup'
			for p in /proc/[0-9]*; do
				# shellcheck disable=SC2046
				set -- $(awk '/^VmRSS/{r=$2}/^VmSwap/{s=$2}END{print r+0,s+0}' "$p/status" 2>/dev/null)
				[ -n "$1" ] && [ $(($1 + $2)) -gt 10240 ] &&
					echo "$(($1 + $2)) $1 $2 ${p#/proc/} $(tr ' ' '_' <"$p/comm" 2>/dev/null) $(head -1 "$p/cgroup" 2>/dev/null | cut -c1-40)"
			done | sort -rn | head -30
			echo '--- containers'
			docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null | head -40
		} >"$file" 2>/dev/null
		ls -t "$dir"/mem-guard-*.log 2>/dev/null | tail -n +$((KEEP + 1)) | xargs rm -f 2>/dev/null
	) &
}

echo -900 >/proc/$$/oom_score_adj 2>/dev/null
log "started observe-only snap=${SNAP_KB}kB/${SNAP_PSI}% alert=${ALERT_KB}kB/${ALERT_PSI}% interval=${INTERVAL}s"

bad=0
alerted=0
loops=0
since_snap=$SNAP_EVERY
while :; do
	kb=$(meminfo_kb MemAvailable)
	psi=$(psi_full)
	if [ $((loops % TREND_EVERY)) -eq 0 ]; then
		echo "$(date +%m-%dT%H:%M:%S) avail_mb=$((kb / 1024)) swapfree_mb=$(($(meminfo_kb SwapFree) / 1024)) psi_full60=$psi" \
			>>"$(log_dir)/mem-guard.trend" 2>/dev/null
	fi
	since_snap=$((since_snap + 1))
	if [ "$kb" -lt "$SNAP_KB" ] || [ "$psi" -ge "$SNAP_PSI" ]; then
		if [ "$since_snap" -ge "$SNAP_EVERY" ]; then
			snapshot
			since_snap=0
			log "tight: available ${kb} kB, memory full avg60 ${psi}%, snapshot saved"
		fi
	fi
	if [ "$kb" -lt "$ALERT_KB" ] || [ "$psi" -ge "$ALERT_PSI" ]; then
		bad=$((bad + 1))
		if [ "$bad" -ge "$NEED" ] && [ "$alerted" = 0 ]; then
			alert "内存吃紧" "可用 $((kb / 1024)) MiB，内存压力 ${psi}%，快照在 $(log_dir)/mem-guard-*.log。没有停止任何服务。"
			alerted=1
		fi
	else
		bad=0
		alerted=0
	fi
	loops=$((loops + 1))
	[ "$MAX_LOOPS" -gt 0 ] && [ "$loops" -ge "$MAX_LOOPS" ] && break
	sleep "$INTERVAL"
done
