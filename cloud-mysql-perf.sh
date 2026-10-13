#!/usr/bin/env bash
#
# cloud-mysql-perf.sh - OLTP performance test harness for managed MySQL services.
#
#   sysbench  oltp_read_write : 10 tables x 10M / 20M / 40M rows per table
#   HammerDB  TPC-C (TPROC-C) : 160 / 320 / 640 warehouses
#
#   Data sets        : one measured sweep per size (--table-size, --warehouses)
#   Thread/VU levels : 1 2 4 8      (--threads)
#   Ramp-up          : 5 min        (--rampup)
#   Measured run     : 20 min       (--duration)
#   Runs             : 3            (--runs)
#
# Server-side state (global variables and global status before/during/after
# every run) is collected into the results directory; SHOW ENGINE INNODB STATUS
# is only captured with --innodb-status.
#
set -euo pipefail

SCRIPT_NAME=${0##*/}

# ---------------------------------------------------------------- defaults ---
PROVIDER=""
DB_HOST=""
DB_PORT=3306
DB_USER=""
DB_PASS=""
DB_SOCKET=""

RUNS=3
THREAD_LIST="1 2 4 8"
RAMPUP_MIN=5
DURATION_MIN=20

BENCHMARKS="sysbench,hammerdb"

SB_DB="sbtest"
SB_TABLES=10
SB_TABLE_SIZE_LIST="10M 20M 40M"   # rows per table, one data set per value
SB_PREPARE_THREADS=10

HDB_HOME=${HAMMERDB_HOME:-/opt/HammerDB-6.0}
HDB_DB="tpcc"
HDB_WAREHOUSE_LIST="160 320 640"   # one data set per value
HDB_BUILD_VU=16
HDB_ALLWAREHOUSE="true"
HDB_NO_STORED_PROCS="false"  # auto | true | false

OUT_ROOT="results"
LABEL=""
SAMPLE_INTERVAL=10
COLLECT_INNODB=0
USE_SSL=0
REBUILD="run"                # run | dataset | never
PREPARE_ONLY=0
CLEANUP_AFTER=0
DRY_RUN=0
ASSUME_YES=0

usage() {
    cat <<EOF
$SCRIPT_NAME - sysbench + HammerDB TPC-C benchmark driver for managed MySQL

Required:
      --provider NAME          cloud provider, e.g. digitalocean, linode, hetzner.
                               Names the results directory, prefixes every file
                               it contains and is the first column of both CSVs.
  -H, --host HOST              MySQL host
  -u, --user USER              MySQL user
  -p, --password PASS          MySQL password (or set MYSQL_PWD / --password-file)

Connection:
  -P, --port PORT              MySQL port (default $DB_PORT)
      --password-file FILE     read the password from FILE (first line)
      --socket PATH            unix socket for HammerDB (only needed when the
                               server really is local - HammerDB connects over
                               the socket whenever the host is localhost/127.0.0.1)
      --ssl                    require TLS for the mysql client and sysbench

Workload:
  -r, --runs N                 repetitions of the full matrix (default $RUNS)
  -t, --threads "1 2 4 8"      thread / virtual-user levels (default "$THREAD_LIST")
      --rampup MIN             ramp-up, discarded from results (default $RAMPUP_MIN)
      --duration MIN           measured run length (default $DURATION_MIN)
  -b, --benchmarks LIST        sysbench,hammerdb (default $BENCHMARKS)

sysbench:
      --sysbench-db NAME       database (default $SB_DB)
      --tables N               tables per data set (default $SB_TABLES)
      --table-size "LIST"      rows per table - one data set per value, K/M
                               suffixes allowed (default "$SB_TABLE_SIZE_LIST")
      --prepare-threads N      threads used by 'sysbench prepare' (default $SB_PREPARE_THREADS)

HammerDB:
      --hammerdb-home DIR      HammerDB install (default $HDB_HOME)
      --hammerdb-db NAME       database (default $HDB_DB)
      --warehouses "LIST"      warehouses - one data set per value
                               (default "$HDB_WAREHOUSE_LIST")
      --build-vu N             virtual users used to build the schema (default $HDB_BUILD_VU)
      --no-allwarehouse        each VU stays on its home warehouse
      --no-stored-procs yes|no|auto
                               build the schema without stored procedures
                               (default no; auto enables it when the server has
                               binary logging on and log_bin_trust_function_creators off)

Schema / output:
      --rebuild WHEN           run     drop and reload before every measured
                                       run (default)
                               dataset reload once per data set and run, then
                                       sweep the thread levels on it
                               never   reuse whatever is already loaded
      --skip-prepare           alias for --rebuild never
      --prepare-only           load the first data set only, run no benchmark
      --cleanup-after          drop both data sets when finished
  -o, --outdir DIR             results root (default $OUT_ROOT)
  -l, --label TEXT             label appended to the results directory name
      --sample-interval SEC    SHOW GLOBAL STATUS sampling during runs, 0=off (default $SAMPLE_INTERVAL)
      --innodb-status          also capture SHOW ENGINE INNODB STATUS before and
                               after every run (off by default; needs PROCESS)
  -n, --dry-run                print what would run, touch nothing
  -y, --yes                    do not pause for the runtime estimate
  -h, --help                   this text

Example:
  $SCRIPT_NAME --provider digitalocean -H db.example.com -P 25060 \\
      -u doadmin -p secret --label 2vcpu-4gb --runs 3

Credentials are passed to both tools through 0600 files in a private temp
directory, so they do not appear in the process list.
EOF
}

die() { printf '%s: %s\n' "$SCRIPT_NAME" "$*" >&2; exit 1; }

# ------------------------------------------------------------------- args ----
while [[ $# -gt 0 ]]; do
    case $1 in
        --provider)           PROVIDER=$2; shift 2 ;;
        -H|--host)            DB_HOST=$2; shift 2 ;;
        -P|--port)            DB_PORT=$2; shift 2 ;;
        -u|--user)            DB_USER=$2; shift 2 ;;
        -p|--password)        DB_PASS=$2; shift 2 ;;
        --password-file)      DB_PASS=$(head -n1 "$2"); shift 2 ;;
        --socket)             DB_SOCKET=$2; shift 2 ;;
        --ssl)                USE_SSL=1; shift ;;
        -r|--runs)            RUNS=$2; shift 2 ;;
        -t|--threads)         THREAD_LIST=$2; shift 2 ;;
        --rampup)             RAMPUP_MIN=$2; shift 2 ;;
        --duration)           DURATION_MIN=$2; shift 2 ;;
        -b|--benchmarks)      BENCHMARKS=$2; shift 2 ;;
        --sysbench-db)        SB_DB=$2; shift 2 ;;
        --tables)             SB_TABLES=$2; shift 2 ;;
        --table-size|--table-sizes) SB_TABLE_SIZE_LIST=$2; shift 2 ;;
        --prepare-threads)    SB_PREPARE_THREADS=$2; shift 2 ;;
        --hammerdb-home)      HDB_HOME=$2; shift 2 ;;
        --hammerdb-db)        HDB_DB=$2; shift 2 ;;
        --warehouses)         HDB_WAREHOUSE_LIST=$2; shift 2 ;;
        --build-vu)           HDB_BUILD_VU=$2; shift 2 ;;
        --no-allwarehouse)    HDB_ALLWAREHOUSE="false"; shift ;;
        --no-stored-procs)
            case $2 in
                yes|true)  HDB_NO_STORED_PROCS="true" ;;
                no|false)  HDB_NO_STORED_PROCS="false" ;;
                auto)      HDB_NO_STORED_PROCS="auto" ;;
                *) die "--no-stored-procs expects yes|no|auto" ;;
            esac
            shift 2 ;;
        --rebuild)
            case $2 in
                run|dataset|never) REBUILD=$2 ;;
                *) die "--rebuild expects run|dataset|never" ;;
            esac
            shift 2 ;;
        --skip-prepare)       REBUILD="never"; shift ;;
        --prepare-only)       PREPARE_ONLY=1; shift ;;
        --cleanup-after)      CLEANUP_AFTER=1; shift ;;
        -o|--outdir)          OUT_ROOT=$2; shift 2 ;;
        -l|--label)           LABEL=$2; shift 2 ;;
        --sample-interval)    SAMPLE_INTERVAL=$2; shift 2 ;;
        --innodb-status)      COLLECT_INNODB=1; shift ;;
        --no-innodb-status)   COLLECT_INNODB=0; shift ;;
        -n|--dry-run)         DRY_RUN=1; shift ;;
        -y|--yes)             ASSUME_YES=1; shift ;;
        -h|--help)            usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done

[[ -n $PROVIDER ]] || { usage >&2; die "--provider is required"; }
# The provider name ends up in path names, so keep it to safe characters.
PROVIDER=${PROVIDER//[^A-Za-z0-9._-]/-}
[[ $PROVIDER =~ [A-Za-z0-9] ]] || die "--provider must contain alphanumeric characters"

[[ -n $DB_HOST ]] || { usage >&2; die "--host is required"; }
[[ -n $DB_USER ]] || { usage >&2; die "--user is required"; }
[[ -n $DB_PASS ]] || DB_PASS=${MYSQL_PWD:-}
[[ -n $DB_PASS ]] || { usage >&2; die "--password / --password-file / MYSQL_PWD is required"; }

RUN_SYSBENCH=0
RUN_HAMMERDB=0
IFS=',' read -r -a _benches <<<"$BENCHMARKS"
for b in "${_benches[@]}"; do
    case ${b// /} in
        sysbench) RUN_SYSBENCH=1 ;;
        hammerdb|tpcc|tproc-c) RUN_HAMMERDB=1 ;;
        "") ;;
        *) die "unknown benchmark: $b" ;;
    esac
done
(( RUN_SYSBENCH || RUN_HAMMERDB )) || die "no benchmark selected"

# "40M" -> 40000000, "500k" -> 500000, "250" -> 250
to_number() {
    local v=${1//,/}
    case $v in
        *[Mm]) v=${v%[Mm]}; [[ $v =~ ^[0-9]+$ ]] || die "not a number: $1"; echo $(( v * 1000000 )) ;;
        *[Kk]) v=${v%[Kk]}; [[ $v =~ ^[0-9]+$ ]] || die "not a number: $1"; echo $(( v * 1000 )) ;;
        *) [[ $v =~ ^[0-9]+$ ]] || die "not a number: $1"; echo "$v" ;;
    esac
}

# 40000000 -> 40M, 500000 -> 500K; used in file names and log lines.
num_label() {
    local n=$1
    if (( n >= 1000000 && n % 1000000 == 0 )); then echo "$(( n / 1000000 ))M"
    elif (( n >= 1000 && n % 1000 == 0 )); then echo "$(( n / 1000 ))K"
    else echo "$n"; fi
}

# Data-set dimensions: one measured sweep of the thread matrix per value.
SB_SIZES=()
for v in ${SB_TABLE_SIZE_LIST//,/ }; do SB_SIZES+=("$(to_number "$v")"); done
HDB_WAREHOUSES=()
for v in ${HDB_WAREHOUSE_LIST//,/ }; do HDB_WAREHOUSES+=("$(to_number "$v")"); done
if (( RUN_SYSBENCH )) && (( ${#SB_SIZES[@]} == 0 )); then die "--table-size is empty"; fi
if (( RUN_HAMMERDB )) && (( ${#HDB_WAREHOUSES[@]} == 0 )); then die "--warehouses is empty"; fi

if [[ $REBUILD == never ]]; then
    if (( RUN_SYSBENCH && ${#SB_SIZES[@]} > 1 )) || (( RUN_HAMMERDB && ${#HDB_WAREHOUSES[@]} > 1 )); then
        echo "$SCRIPT_NAME: WARN: --rebuild never with several data-set sizes - every sweep will run against whatever is loaded" >&2
    fi
fi

RAMPUP_SEC=$(( RAMPUP_MIN * 60 ))
DURATION_SEC=$(( DURATION_MIN * 60 ))

command -v mysql >/dev/null || die "mysql client not found"
if (( RUN_SYSBENCH )); then
    command -v sysbench >/dev/null || die "sysbench not found"
fi
if (( RUN_HAMMERDB )); then
    [[ -x $HDB_HOME/hammerdbcli ]] || die "hammerdbcli not found in $HDB_HOME"
fi

# ------------------------------------------------------- private temp dir ----
umask 077
TMPSEC=$(mktemp -d "${TMPDIR:-/tmp}/cloud-mysql-perf.XXXXXX")
SAMPLER_PID=""

cleanup() {
    [[ -n $SAMPLER_PID ]] && kill "$SAMPLER_PID" 2>/dev/null || true
    rm -rf "$TMPSEC"
}
trap cleanup EXIT INT TERM

# Both option-file formats treat '#' as a comment, so values are quoted.
# MySQL additionally processes backslash escapes inside double quotes.
MY_PASS=${DB_PASS//\\/\\\\}
MY_PASS=${MY_PASS//\"/\\\"}
if [[ $DB_PASS == *'"'* || $DB_PASS == *'\'* ]]; then
    echo "$SCRIPT_NAME: WARN: sysbench may mis-read a password containing a quote or backslash" >&2
fi
if (( RUN_HAMMERDB )) && [[ $DB_PASS == *'{'* || $DB_PASS == *'}'* ]]; then
    echo "$SCRIPT_NAME: WARN: a password containing braces breaks the generated Tcl script" >&2
fi

MY_CNF="$TMPSEC/my.cnf"
{
    echo "[client]"
    echo "host=$DB_HOST"
    echo "port=$DB_PORT"
    echo "user=\"$DB_USER\""
    echo "password=\"$MY_PASS\""
    if [[ -n $DB_SOCKET ]]; then echo "socket=\"$DB_SOCKET\""; fi
    if (( USE_SSL )); then echo "ssl-mode=REQUIRED"; fi
} >"$MY_CNF"

SB_CNF="$TMPSEC/sysbench.cnf"
{
    echo "[sysbench]"
    echo "db-driver=mysql"
    echo "mysql-host=$DB_HOST"
    echo "mysql-port=$DB_PORT"
    echo "mysql-user=\"$DB_USER\""
    echo "mysql-password=\"$DB_PASS\""
    echo "mysql-db=\"$SB_DB\""
    if (( USE_SSL )); then echo "mysql-ssl=on"; fi
} >"$SB_CNF"

mysql_q()  { mysql --defaults-extra-file="$MY_CNF" --batch --raw --skip-column-names -e "$1"; }
mysql_qv() { mysql --defaults-extra-file="$MY_CNF" --batch --raw -e "$1"; }

# Filter for anything HammerDB writes to a log. 'diset' echoes the value it
# sets, and it may echo it Tcl-escaped (sm\#ok), so whole lines mentioning
# mysql_pass are dropped in addition to a literal match on the password.
# HammerDB also prefixes most lines with CR.
scrub() {
    awk -v pw="$DB_PASS" '
        { gsub(/\r/,"")
          if ($0 ~ /mysql_pass/) { print "<password line redacted>"; next }
          while (pw != "" && (p = index($0, pw)) > 0)
              $0 = substr($0, 1, p-1) "<redacted>" substr($0, p + length(pw))
          print }'
}

# --------------------------------------------------------- results layout ----
# Every file is prefixed with the provider so that results from several
# providers can be collected in one place without colliding.
STAMP=$(date -u +%Y%m%d-%H%M%S)
OUT="$OUT_ROOT/$PROVIDER-$STAMP${LABEL:+-$LABEL}"
PFX="$PROVIDER"
umask 022

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$OUT/${PFX}_run.log"; }
hdr() { printf '\n== %s ==\n' "$*" | tee -a "$OUT/${PFX}_run.log"; }

if (( DRY_RUN )); then
    OUT="$TMPSEC/dryrun"
fi
mkdir -p "$OUT/server" "$OUT/sysbench" "$OUT/hammerdb"
# hammerdbcli runs with cwd=$HDB_HOME, so every path handed to it (TMP included)
# has to be absolute.
OUT=$(cd "$OUT" && pwd)
: >"$OUT/${PFX}_run.log"

# --------------------------------------------------------- runtime budget ----
read -r -a THREADS <<<"${THREAD_LIST//,/ }"
sb_cfg=$(( RUN_SYSBENCH * RUNS * ${#SB_SIZES[@]} * ${#THREADS[@]} ))
hdb_cfg=$(( RUN_HAMMERDB * RUNS * ${#HDB_WAREHOUSES[@]} * ${#THREADS[@]} ))
total_cfg=$(( sb_cfg + hdb_cfg ))
total_min=$(( total_cfg * (RAMPUP_MIN + DURATION_MIN) ))

case $REBUILD in
    run)     rebuilds=$total_cfg; rebuild_txt="dropped and reloaded before every measured run" ;;
    dataset) rebuilds=$(( RUNS * (RUN_SYSBENCH * ${#SB_SIZES[@]} + RUN_HAMMERDB * ${#HDB_WAREHOUSES[@]}) ))
             rebuild_txt="dropped and reloaded once per data set and run" ;;
    never)   rebuilds=0; rebuild_txt="off (--rebuild never) - existing data sets are reused" ;;
esac

cat >"$OUT/${PFX}_config.txt" <<EOF
provider          $PROVIDER
started_utc       $(date -u +%Y-%m-%dT%H:%M:%SZ)
client_host       $(hostname)
client_cpus       $(nproc)
client_sysbench   $(command -v sysbench >/dev/null && sysbench --version || echo n/a)
client_mysql      $(mysql --version)
hammerdb_home     $HDB_HOME
target            $DB_USER@$DB_HOST:$DB_PORT (ssl=$USE_SSL)
benchmarks        $BENCHMARKS
runs              $RUNS
threads           $THREAD_LIST
rampup_min        $RAMPUP_MIN
duration_min      $DURATION_MIN
sysbench_schema   $SB_DB, $SB_TABLES tables x {${SB_SIZES[*]}} rows per table
hammerdb_schema   $HDB_DB, {${HDB_WAREHOUSES[*]}} warehouses, allwarehouse=$HDB_ALLWAREHOUSE
configurations    $total_cfg (sysbench $sb_cfg, hammerdb $hdb_cfg)
schema_rebuild    $rebuild_txt
status_sampling   $( (( SAMPLE_INTERVAL > 0 )) && echo "every ${SAMPLE_INTERVAL}s" || echo off )
innodb_status     $( (( COLLECT_INNODB )) && echo "before/after every run" || echo "off (--innodb-status to enable)" )
estimated_runtime ${total_min} min (~$(( total_min / 60 ))h, excluding $rebuilds schema rebuilds)
EOF
cat "$OUT/${PFX}_config.txt"

if (( ! DRY_RUN && ! ASSUME_YES && ! PREPARE_ONLY )); then
    printf '\nStarting in 10s - Ctrl-C to abort.\n'
    read -r -t 10 -n 1 _ || true
fi

# --------------------------------------------------- server state capture ----
# $1 = phase tag, e.g. "baseline" or "sysbench_r1_t4_before"
collect_status() {
    local tag=$1
    mysql_q 'SHOW GLOBAL STATUS' >"$OUT/server/${PFX}_status_$tag.tsv" 2>>"$OUT/server/${PFX}_errors.log" || true
}

collect_variables() {
    local tag=$1
    mysql_q 'SHOW GLOBAL VARIABLES' >"$OUT/server/${PFX}_variables_$tag.tsv" 2>>"$OUT/server/${PFX}_errors.log" || true
}

# Off by default: it needs PROCESS, which managed services often withhold, and
# the dumps are large. Enable with --innodb-status.
collect_innodb() {
    local tag=$1
    (( COLLECT_INNODB )) || return 0
    mysql --defaults-extra-file="$MY_CNF" -e 'SHOW ENGINE INNODB STATUS\G' \
        >"$OUT/server/${PFX}_innodb_status_$tag.txt" 2>>"$OUT/server/${PFX}_errors.log" || \
        log "WARN: SHOW ENGINE INNODB STATUS failed for $tag (PROCESS privilege?)"
}

# $1 = optional tag, so the loaded size of every data set can be recorded
collect_server_info() {
    local tag=${1:-}
    {
        echo "-- version"
        mysql_qv 'SELECT VERSION() AS version, @@version_comment AS comment, @@hostname AS hostname'
        echo "-- sizing"
        mysql_qv 'SELECT @@innodb_buffer_pool_size AS innodb_buffer_pool_size,
                         @@max_connections AS max_connections,
                         @@innodb_flush_log_at_trx_commit AS flush_log_at_trx_commit,
                         @@sync_binlog AS sync_binlog,
                         @@innodb_io_capacity AS innodb_io_capacity,
                         @@log_bin AS log_bin,
                         @@transaction_isolation AS transaction_isolation'
        echo "-- schema sizes"
        mysql_qv "SELECT table_schema,
                         COUNT(*) AS tables,
                         ROUND(SUM(data_length)/1024/1024/1024,2) AS data_gb,
                         ROUND(SUM(index_length)/1024/1024/1024,2) AS index_gb
                  FROM information_schema.tables
                  WHERE table_schema IN ('$SB_DB','$HDB_DB')
                  GROUP BY table_schema"
    } >"$OUT/server/${PFX}_server_info${tag:+_$tag}.txt" 2>>"$OUT/server/${PFX}_errors.log" || true
}

# before-file after-file -> delta of every numeric counter that moved
status_delta() {
    awk -F'\t' '
        NR==FNR { before[$1]=$2; next }
        ($1 in before) && before[$1] ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ {
            d = $2 - before[$1]
            if (d != 0) printf "%s\t%s\t%s\t%s\n", $1, before[$1], $2, d
        }' "$1" "$2" | sort
}

start_sampler() {
    local out=$1
    (( SAMPLE_INTERVAL > 0 )) || return 0
    printf 'ts\tvariable\tvalue\n' >"$out"
    (
        while :; do
            ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
            { mysql_q 'SHOW GLOBAL STATUS' 2>/dev/null || true; } | awk -F'\t' -v ts="$ts" '
                $1 ~ /^(Queries|Questions|Com_select|Com_insert|Com_update|Com_delete|Com_commit|Com_rollback|Threads_running|Threads_connected|Innodb_rows_read|Innodb_rows_inserted|Innodb_rows_updated|Innodb_rows_deleted|Innodb_buffer_pool_read_requests|Innodb_buffer_pool_reads|Innodb_buffer_pool_pages_dirty|Innodb_buffer_pool_pages_free|Innodb_data_reads|Innodb_data_writes|Innodb_log_waits|Innodb_row_lock_waits|Innodb_row_lock_time|Slow_queries|Aborted_clients)$/ \
                    { print ts "\t" $1 "\t" $2 }' >>"$out"
            sleep "$SAMPLE_INTERVAL"
        done
    ) &
    SAMPLER_PID=$!
}

stop_sampler() {
    [[ -n $SAMPLER_PID ]] || return 0
    kill "$SAMPLER_PID" 2>/dev/null || true
    wait "$SAMPLER_PID" 2>/dev/null || true
    SAMPLER_PID=""
}

# Wraps one measured run: server snapshots + status sampling around $@.
# $1 = tag, rest = command
measure() {
    local tag=$1; shift
    local dir="$OUT/server"
    if (( DRY_RUN )); then "$@"; return $?; fi
    collect_status "${tag}_before"
    collect_innodb "${tag}_before"
    start_sampler "$dir/${PFX}_samples_$tag.tsv"
    local rc=0
    "$@" || rc=$?
    stop_sampler
    collect_status "${tag}_after"
    collect_innodb "${tag}_after"
    status_delta "$dir/${PFX}_status_${tag}_before.tsv" "$dir/${PFX}_status_${tag}_after.tsv" \
        >"$dir/${PFX}_status_delta_$tag.tsv" 2>/dev/null || true
    return $rc
}

# ----------------------------------------------------------- preflight ----
hdr "PREFLIGHT"
if (( DRY_RUN )); then
    log "dry run: skipping connection check"
else
    mysql_q 'SELECT 1' >/dev/null || die "cannot connect to $DB_HOST:$DB_PORT as $DB_USER"
    log "connected to $DB_HOST:$DB_PORT"
    if (( RUN_HAMMERDB )) && [[ -z $DB_SOCKET && $DB_HOST =~ ^(localhost|127\.0\.0\.1|::1)$ ]]; then
        log "WARN: HammerDB connects over the unix socket for host '$DB_HOST'."
        log "WARN: pass --socket PATH, or use the server's routable address/DNS name."
    fi
    collect_variables baseline
    collect_status baseline
    collect_innodb baseline
    collect_server_info

    if [[ $HDB_NO_STORED_PROCS == auto ]]; then
        log_bin=$(mysql_q 'SELECT @@log_bin' || echo 0)
        trust=$(mysql_q 'SELECT @@log_bin_trust_function_creators' 2>/dev/null || echo 1)
        if [[ $log_bin == 1 && $trust == 0 ]]; then
            HDB_NO_STORED_PROCS="true"
            log "binary logging on with log_bin_trust_function_creators=OFF -> building TPC-C without stored procedures"
        else
            HDB_NO_STORED_PROCS="false"
        fi
    fi
fi
# 'auto' is not a value HammerDB accepts; fall back if preflight was skipped.
if [[ $HDB_NO_STORED_PROCS == auto ]]; then HDB_NO_STORED_PROCS="false"; fi

# ------------------------------------------------------------- sysbench ------
SB_CSV="$OUT/${PFX}_sysbench_results.csv"
# --table-size varies per data set, so it is added by each function.
sb_common=(--config-file="$SB_CNF" --tables="$SB_TABLES")

# Drops the sysbench data set. DROP DATABASE is the cheapest and the most
# thorough way, but not every managed service grants it, so fall back to the
# tool's own cleanup (which only knows about the current --tables).
# $1 = rows per table, $2 = tag used in the log file name
sysbench_drop() {
    local rows=$1 tag=$2
    if (( DRY_RUN )); then return 0; fi
    if mysql_q "DROP DATABASE IF EXISTS \`$SB_DB\`" 2>>"$OUT/server/${PFX}_errors.log"; then return 0; fi
    log "WARN: cannot drop database $SB_DB - falling back to 'sysbench cleanup'"
    sysbench "${sb_common[@]}" --table-size="$rows" oltp_read_write cleanup \
        >>"$OUT/sysbench/${PFX}_cleanup_$tag.log" 2>&1 || true
}

# Rebuilds the data set from scratch; $1 = rows per table, $2 = file name tag.
sysbench_prepare() {
    local rows=$1 tag=$2
    hdr "SYSBENCH PREPARE $tag ($SB_TABLES tables x $rows rows)"
    local cmd=(sysbench "${sb_common[@]}" --table-size="$rows" --threads="$SB_PREPARE_THREADS" oltp_read_write prepare)
    log "dropping $SB_DB and running sysbench oltp_read_write prepare ($SB_TABLES x $rows rows, threads=$SB_PREPARE_THREADS)"
    if (( DRY_RUN )); then printf '  %s\n' "${cmd[*]}" | sed 's/--config-file=[^ ]*/--config-file=<creds>/'; return 0; fi
    sysbench_drop "$rows" "$tag"
    mysql_q "CREATE DATABASE IF NOT EXISTS \`$SB_DB\`" \
        || log "WARN: could not create $SB_DB - assuming it exists"
    "${cmd[@]}" >"$OUT/sysbench/${PFX}_prepare_$tag.log" 2>&1 \
        || { tail -20 "$OUT/sysbench/${PFX}_prepare_$tag.log"; die "sysbench prepare failed"; }
    log "prepare done ($(mysql_q "SELECT ROUND(SUM(data_length+index_length)/1024/1024/1024,2) FROM information_schema.tables WHERE table_schema='$SB_DB'" 2>/dev/null || echo '?') GB)"
}

# $1 = run number, $2 = rows per table, $3 = threads
sysbench_exec() {
    local run=$1 rows=$2 thr=$3
    local out="$OUT/sysbench/${PFX}_run${run}_s$(num_label "$rows")_t${thr}.log"
    # One continuous sysbench run: --report-checkpoints resets all counters at
    # the end of the ramp-up, so the final report covers the measured window
    # only, without dropping and re-establishing connections.
    local cmd=(sysbench "${sb_common[@]}"
        --table-size="$rows"
        --threads="$thr"
        --time=$(( RAMPUP_SEC + DURATION_SEC ))
        --report-interval=10
        --percentile=95
        --histogram=on
        --db-ps-mode=auto)
    if (( RAMPUP_SEC > 0 )); then cmd+=(--report-checkpoints="$RAMPUP_SEC"); fi
    cmd+=(oltp_read_write run)
    if (( DRY_RUN )); then printf '  %s\n' "${cmd[*]}" | sed 's/--config-file=[^ ]*/--config-file=<creds>/'; return 0; fi
    "${cmd[@]}" >"$out" 2>&1 || log "WARN: sysbench run $run rows=$rows t=$thr exited non-zero"
    sysbench_parse "$run" "$rows" "$thr" "$out" >>"$SB_CSV"
}

# Takes the LAST statistics block of the log, i.e. the post-ramp-up report.
sysbench_parse() {
    local run=$1 rows=$2 thr=$3 file=$4
    awk -v provider="$PROVIDER" -v run="$run" -v tables="$SB_TABLES" -v rows="$rows" -v thr="$thr" '
        /^SQL statistics:/            { inblock=1 }
        inblock && /^ *transactions:/ { gsub(/[()]/,""); tps=$3; txn=$2 }
        inblock && /^ *queries:/      { gsub(/[()]/,""); qps=$3; qry=$2 }
        inblock && /ignored errors:/  { gsub(/[()]/,""); errs=$3 }
        /total time:/                 { gsub(/s$/,"",$3); ttime=$3 }
        /^ *min:/                     { lmin=$2 }
        /^ *avg:/                     { lavg=$2 }
        /^ *max:/                     { lmax=$2 }
        /percentile:/                 { p95=$3 }
        END {
            printf "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n",
                   provider, "sysbench", run, tables, rows, thr,
                   ttime, txn, tps, qry, qps,
                   lmin, lavg, p95, lmax, (errs==""?0:errs)
        }' "$file"
}

sysbench_cleanup() {
    log "sysbench cleanup"
    sysbench_drop "${SB_SIZES[0]}" final
}

# ------------------------------------------------------------- HammerDB ------
HDB_CSV="$OUT/${PFX}_hammerdb_results.csv"
# HammerDB persists its settings dictionary (including mysql_pass) and its jobs
# database under $TMP, so $TMP stays in the private temp dir and only scrubbed
# text logs are copied into the results directory.
export TMP="$TMPSEC/hammerdb-tmp"
mkdir -p "$TMP"

hdb_conn_block() {
    cat <<EOF
dbset db mysql
dbset bm TPC-C
diset connection mysql_host $DB_HOST
diset connection mysql_port $DB_PORT
${DB_SOCKET:+diset connection mysql_socket $DB_SOCKET}
diset tpcc mysql_user {$DB_USER}
diset tpcc mysql_pass {$DB_PASS}
diset tpcc mysql_dbase $HDB_DB
EOF
}

# Runs a generated tcl script; $1 = script path, $2 = log path.
# 'diset' echoes every value it sets, so the mysql_pass line is redacted on the
# way into the log. pipefail propagates hammerdbcli's exit status.
hdb_cli() {
    local script=$1 log=$2
    ( cd "$HDB_HOME" && TMP="$TMP" ./hammerdbcli auto "$script" ) 2>&1 | scrub >"$log"
}

# Moves the virtual-user log and time-profile written under $TMP into the
# results directory; $1 = file name prefix.
hdb_save_tmp_logs() {
    local prefix=$1 f base
    for f in "$TMP"/hammerdb.log "$TMP"/hdbxtprofile.log; do
        [[ -s $f ]] || continue
        base=${f##*/}
        scrub <"$f" >"$OUT/hammerdb/${PFX}_${prefix}_${base}"
        : >"$f"
    done
}

# Drops the TPC-C schema; $1 = tag used in the log file name. 'deleteschema' is
# only the fallback - DROP DATABASE is faster and also copes with a schema that
# a failed build left half-finished.
hammerdb_drop() {
    local tag=$1
    if (( DRY_RUN )); then return 0; fi
    if mysql_q "DROP DATABASE IF EXISTS \`$HDB_DB\`" 2>>"$OUT/server/${PFX}_errors.log"; then return 0; fi
    log "WARN: cannot drop database $HDB_DB - falling back to HammerDB deleteschema"
    local tcl="$TMPSEC/drop.tcl"
    { hdb_conn_block; echo "deleteschema"; } >"$tcl"
    hdb_cli "$tcl" "$OUT/hammerdb/${PFX}_deleteschema_$tag.log" || true
}

# Rebuilds the TPC-C schema from scratch; $1 = warehouses, $2 = file name tag.
hammerdb_build() {
    local wh=$1 tag=$2
    hdr "HAMMERDB SCHEMA BUILD $tag ($wh warehouses)"
    local tcl="$TMPSEC/build.tcl"
    local build_vu=$(( HDB_BUILD_VU < wh ? HDB_BUILD_VU : wh ))
    {
        hdb_conn_block
        cat <<EOF
diset tpcc mysql_count_ware $wh
diset tpcc mysql_num_vu $build_vu
diset tpcc mysql_storage_engine innodb
diset tpcc mysql_partition false
diset tpcc mysql_no_stored_procs $HDB_NO_STORED_PROCS
diset tpcc mysql_history_pk true
buildschema
EOF
    } >"$tcl"
    sed "s/^diset tpcc mysql_pass .*/diset tpcc mysql_pass <redacted>/" "$tcl" >"$OUT/hammerdb/${PFX}_build_$tag.tcl"
    log "dropping $HDB_DB and building $wh warehouses with $build_vu virtual users (no_stored_procs=$HDB_NO_STORED_PROCS)"
    if (( DRY_RUN )); then sed 's/mysql_pass .*/mysql_pass <redacted>/' "$tcl"; return 0; fi
    hammerdb_drop "$tag"
    local log_file="$OUT/hammerdb/${PFX}_build_$tag.log"
    hdb_cli "$tcl" "$log_file" \
        || { hdb_save_tmp_logs "build_$tag"; tail -30 "$log_file"; die "HammerDB schema build failed"; }
    hdb_save_tmp_logs "build_$tag"
    grep -qi "SCHEMA COMPLETE\|Schema Complete" "$log_file" \
        || log "WARN: 'schema complete' not found in ${log_file##*/} - check it before trusting results"
    log "schema build done"
}

# $1 = run number, $2 = warehouses, $3 = virtual users
hammerdb_exec() {
    local run=$1 wh=$2 vu=$3
    local name="run${run}_w${wh}_vu${vu}"
    local tcl="$TMPSEC/$name.tcl"
    local log="$OUT/hammerdb/${PFX}_$name.log"
    {
        hdb_conn_block
        cat <<EOF
diset tpcc mysql_count_ware $wh
diset tpcc mysql_driver timed
diset tpcc mysql_rampup $RAMPUP_MIN
diset tpcc mysql_duration $DURATION_MIN
diset tpcc mysql_allwarehouse $HDB_ALLWAREHOUSE
diset tpcc mysql_timeprofile true
diset tpcc mysql_raiseerror true
diset tpcc mysql_keyandthink false
loadscript
vuset vu $vu
vuset logtotemp 1
vucreate
tcstart
set jobid [ vurun ]
vudestroy
tcstop
# vurun returns either the job id or "Jobid = <id>"; accept both.
set jid \$jobid
if { [ llength [ split \$jobid = ] ] > 1 } {
    set jid [ string trim [ lindex [ split \$jobid = ] 1 ] ]
}
puts "JOBID \$jid"
if { [ catch {
    puts "TRANSACTION RESPONSE TIMES"
    job \$jid timing
    puts "TRANSACTION COUNT"
    job \$jid tcount
    puts "HAMMERDB RESULT"
    job \$jid result
} err ] } { puts "RESULT EXTRACTION FAILED: \$err" }
EOF
    } >"$tcl"
    sed "s/^diset tpcc mysql_pass .*/diset tpcc mysql_pass <redacted>/" "$tcl" >"$OUT/hammerdb/${PFX}_$name.tcl"
    if (( DRY_RUN )); then printf '  hammerdbcli auto %s.tcl (%s warehouses, rampup %s min, duration %s min)\n' "$name" "$wh" "$RAMPUP_MIN" "$DURATION_MIN"; return 0; fi
    hdb_cli "$tcl" "$log" || log "WARN: hammerdbcli run $run wh=$wh vu=$vu exited non-zero"
    hdb_save_tmp_logs "$name"
    hammerdb_parse "$run" "$wh" "$vu" "$log" >>"$HDB_CSV"
}

hammerdb_parse() {
    local run=$1 wh=$2 vu=$3 file=$4
    # "System achieved 12345 NOPM from 34567 MySQL TPM"
    awk -v provider="$PROVIDER" -v run="$run" -v wh="$wh" -v vu="$vu" '
        /System achieved/ {
            for (i=1;i<=NF;i++) {
                if ($i=="NOPM") nopm=$(i-1)
                if ($i ~ /^TPM/) for (j=i-1;j>=1;j--) if ($j ~ /^[0-9]+$/) { tpm=$j; break }
            }
        }
        /FINISHED SUCCESS/           { ok=1 }
        /ERROR|FINISHED FAILED/      { errs++ }
        END {
            printf "%s,%s,%s,%s,%s,%s,%s,%s,%s\n",
                   provider, "hammerdb_tpcc", run, wh, vu,
                   (nopm==""?"NA":nopm), (tpm==""?"NA":tpm),
                   (ok?"ok":"check_log"), (errs==""?0:errs)
        }' "$file"
}

hammerdb_cleanup() {
    log "HammerDB schema drop"
    hammerdb_drop final
}

# ------------------------------------------------------------------ main -----
if (( PREPARE_ONLY )); then
    if (( RUN_SYSBENCH && ${#SB_SIZES[@]} > 1 )) || (( RUN_HAMMERDB && ${#HDB_WAREHOUSES[@]} > 1 )); then
        log "--prepare-only: only the first data set of each list is loaded"
    fi
    if (( RUN_SYSBENCH )); then sysbench_prepare "${SB_SIZES[0]}" "s$(num_label "${SB_SIZES[0]}")"; fi
    if (( RUN_HAMMERDB )); then hammerdb_build "${HDB_WAREHOUSES[0]}" "w${HDB_WAREHOUSES[0]}"; fi
    if (( ! DRY_RUN )); then collect_server_info; fi
    log "--prepare-only: data sets are ready, exiting"
    exit 0
fi

if [[ $REBUILD == never ]]; then
    log "--rebuild never: reusing the existing $SB_DB / $HDB_DB data sets"
fi

if (( RUN_SYSBENCH )); then
    # elapsed_s covers ramp-up + measured window; every other column is from the
    # post-ramp-up report only.
    printf 'provider,benchmark,run,tables,rows_per_table,threads,elapsed_s,transactions,tps,queries,qps,lat_min_ms,lat_avg_ms,lat_p95_ms,lat_max_ms,errors\n' >"$SB_CSV"
fi
if (( RUN_HAMMERDB )); then
    printf 'provider,benchmark,run,warehouses,vu,nopm,tpm,status,log_errors\n' >"$HDB_CSV"
fi

# True when the data set has to be (re)loaded before the next measured run;
# $1 = thread level about to run.
need_rebuild() {
    case $REBUILD in
        run)     return 0 ;;
        dataset) [[ $1 == "${THREADS[0]}" ]] ;;
        *)       return 1 ;;
    esac
}

# Runs form the outer loop so that an aborted session still holds at least one
# complete pass over every data set, and the data-set size is swept outside the
# thread levels. Each measured run starts from a freshly loaded data set by
# default, so writes from the previous run cannot carry over.
declare -A info_done=()
for run in $(seq 1 "$RUNS"); do
    if (( RUN_SYSBENCH )); then
        for rows in "${SB_SIZES[@]}"; do
            size=$(num_label "$rows")
            for thr in "${THREADS[@]}"; do
                tag="r${run}_s${size}_t${thr}"
                if need_rebuild "$thr"; then sysbench_prepare "$rows" "run${run}_s${size}_t${thr}"; fi
                # Record the loaded size of each data set once.
                if [[ -z ${info_done[sb_$size]:-} ]] && (( ! DRY_RUN )); then
                    collect_server_info "sysbench_s$size"
                    info_done[sb_$size]=1
                fi
                hdr "SYSBENCH run $run/$RUNS ${SB_TABLES}x${size} rows threads=$thr (${RAMPUP_MIN}m ramp + ${DURATION_MIN}m)"
                measure "sysbench_$tag" sysbench_exec "$run" "$rows" "$thr" || true
            done
        done
    fi
    if (( RUN_HAMMERDB )); then
        for wh in "${HDB_WAREHOUSES[@]}"; do
            for vu in "${THREADS[@]}"; do
                tag="r${run}_w${wh}_vu${vu}"
                if need_rebuild "$vu"; then hammerdb_build "$wh" "run${run}_w${wh}_vu${vu}"; fi
                if [[ -z ${info_done[hdb_$wh]:-} ]] && (( ! DRY_RUN )); then
                    collect_server_info "hammerdb_w$wh"
                    info_done[hdb_$wh]=1
                fi
                hdr "HAMMERDB TPC-C run $run/$RUNS ${wh}w vu=$vu (${RAMPUP_MIN}m ramp + ${DURATION_MIN}m)"
                measure "hammerdb_$tag" hammerdb_exec "$run" "$wh" "$vu" || true
            done
        done
    fi
done

if (( ! DRY_RUN )); then
    collect_variables final
    collect_status final
    collect_innodb final
fi

if (( CLEANUP_AFTER && RUN_SYSBENCH )); then sysbench_cleanup; fi
if (( CLEANUP_AFTER && RUN_HAMMERDB )); then hammerdb_cleanup; fi

# --------------------------------------------------------------- summary -----
hdr "SUMMARY"
{
    cat "$OUT/${PFX}_config.txt"
    echo
    if (( RUN_SYSBENCH )) && [[ -s $SB_CSV ]]; then
        echo "sysbench oltp_read_write - mean over $RUNS run(s), $SB_TABLES tables"
        printf '  %-14s %-8s %12s %12s %12s %12s\n' "rows/table" "threads" "tps" "qps" "avg_ms" "p95_ms"
        awk -F, 'NR>1 && $9!="" { k=$5" "$6; t[k]+=$9; q[k]+=$11; l[k]+=$13; p[k]+=$14; n[k]++ }
             END { for (k in n) { split(k, a, " ")
                       printf "  %-14s %-8s %12.2f %12.2f %12.2f %12.2f\n", a[1], a[2],
                              t[k]/n[k], q[k]/n[k], l[k]/n[k], p[k]/n[k] } }' "$SB_CSV" \
            | sort -k1,1n -k2,2n
        echo
    fi
    if (( RUN_HAMMERDB )) && [[ -s $HDB_CSV ]]; then
        echo "HammerDB TPC-C - mean over $RUNS run(s)"
        printf '  %-14s %-8s %12s %12s\n' "warehouses" "vu" "nopm" "tpm"
        awk -F, 'NR>1 && $6!="NA" { k=$4" "$5; o[k]+=$6; m[k]+=$7; n[k]++ }
             END { for (k in n) { split(k, a, " ")
                       printf "  %-14s %-8s %12.1f %12.1f\n", a[1], a[2], o[k]/n[k], m[k]/n[k] } }' "$HDB_CSV" \
            | sort -k1,1n -k2,2n
        echo
    fi
    echo "finished_utc      $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "results           $OUT"
} | tee "$OUT/${PFX}_summary.txt"
