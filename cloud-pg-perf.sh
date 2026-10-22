#!/usr/bin/env bash
#
# cloud-pg-perf.sh - OLTP performance test harness for managed PostgreSQL services.
# PostgreSQL counterpart of cloud-mysql-perf.sh; same matrix, same output layout.
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
# Server-side state (pg_settings, and the pg_stat_* counters before/during/after
# every run) is collected into the results directory; the heavier activity,
# lock and table statistics are only captured with --detail-stats.
#
set -euo pipefail

SCRIPT_NAME=${0##*/}

# ---------------------------------------------------------------- defaults ---
PROVIDER=""
DB_HOST=""
DB_PORT=5432
DB_USER=""
DB_PASS=""
MAINT_DB="postgres"          # database used for CREATE/DROP DATABASE and server state

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
HDB_STORED_PROCS="false"     # PostgreSQL TPROC-C runs client-side SQL by default
TPCC_USER=""                 # defaults to --user, so no role has to be created
TPCC_PASS=""

OUT_ROOT="results"
LABEL=""
SAMPLE_INTERVAL=10
DETAIL_STATS=0
USE_SSL=0
SSL_CA=""
VACUUM_AFTER_LOAD=1
REBUILD="run"                # run | dataset | never
PREPARE_ONLY=0
CLEANUP_AFTER=0
DRY_RUN=0
ASSUME_YES=0

usage() {
    cat <<EOF
$SCRIPT_NAME - sysbench + HammerDB TPC-C benchmark driver for managed PostgreSQL

Required:
      --provider NAME          cloud provider, e.g. digitalocean, linode, hetzner.
                               Names the results directory, prefixes every file
                               it contains and is the first column of both CSVs.
  -H, --host HOST              PostgreSQL host (or a socket directory path)
  -u, --user USER              PostgreSQL user, needs CREATEDB
  -p, --password PASS          password (or set PGPASSWORD / --password-file)

Connection:
  -P, --port PORT              port (default $DB_PORT)
      --password-file FILE     read the password from FILE (first line)
      --maintenance-db NAME    database to connect to for CREATE/DROP DATABASE
                               and for server state (default $MAINT_DB; managed
                               services often use 'defaultdb')
      --ssl                    sslmode=require for psql, sysbench and HammerDB
      --ssl-ca FILE            verify the server certificate against FILE
                               (implies --ssl and sslmode=verify-ca)

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
      --stored-procs yes|no    build the TPC-C schema with stored procedures
                               (default no - the driver issues plain SQL)
      --tpcc-user USER         owner of the TPC-C schema (default: --user, which
                               avoids needing CREATEROLE on a managed service)
      --tpcc-password PASS     password of --tpcc-user (default: --password)

Schema / output:
      --rebuild WHEN           run     drop and reload before every measured
                                       run (default)
                               dataset reload once per data set and run, then
                                       sweep the thread levels on it
                               never   reuse whatever is already loaded
      --skip-prepare           alias for --rebuild never
      --no-vacuum              skip VACUUM ANALYZE after loading a data set
      --prepare-only           load the first data set only, run no benchmark
      --cleanup-after          drop both databases when finished
  -o, --outdir DIR             results root (default $OUT_ROOT)
  -l, --label TEXT             label appended to the results directory name
      --sample-interval SEC    pg_stat sampling during runs, 0=off (default $SAMPLE_INTERVAL)
      --detail-stats           also capture pg_stat_activity, pg_locks and
                               table statistics before and after every run
  -n, --dry-run                print what would run, touch nothing
  -y, --yes                    do not pause for the runtime estimate
  -h, --help                   this text

Example:
  $SCRIPT_NAME --provider digitalocean -H db.example.com -P 25060 \\
      -u doadmin -p secret --maintenance-db defaultdb --ssl --label 2vcpu-4gb

Credentials are passed to psql through a 0600 PGPASSFILE and to sysbench
through a 0600 config file, so they do not appear in the process list.
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
        --maintenance-db)     MAINT_DB=$2; shift 2 ;;
        --ssl)                USE_SSL=1; shift ;;
        --ssl-ca)             SSL_CA=$2; USE_SSL=1; shift 2 ;;
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
        --stored-procs)
            case $2 in
                yes|true)  HDB_STORED_PROCS="true" ;;
                no|false)  HDB_STORED_PROCS="false" ;;
                *) die "--stored-procs expects yes|no" ;;
            esac
            shift 2 ;;
        --tpcc-user)          TPCC_USER=$2; shift 2 ;;
        --tpcc-password)      TPCC_PASS=$2; shift 2 ;;
        --rebuild)
            case $2 in
                run|dataset|never) REBUILD=$2 ;;
                *) die "--rebuild expects run|dataset|never" ;;
            esac
            shift 2 ;;
        --skip-prepare)       REBUILD="never"; shift ;;
        --no-vacuum)          VACUUM_AFTER_LOAD=0; shift ;;
        --prepare-only)       PREPARE_ONLY=1; shift ;;
        --cleanup-after)      CLEANUP_AFTER=1; shift ;;
        -o|--outdir)          OUT_ROOT=$2; shift 2 ;;
        -l|--label)           LABEL=$2; shift 2 ;;
        --sample-interval)    SAMPLE_INTERVAL=$2; shift 2 ;;
        --detail-stats)       DETAIL_STATS=1; shift ;;
        --no-detail-stats)    DETAIL_STATS=0; shift ;;
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
[[ -n $DB_PASS ]] || DB_PASS=${PGPASSWORD:-}
[[ -n $DB_PASS ]] || { usage >&2; die "--password / --password-file / PGPASSWORD is required"; }
[[ -n $TPCC_USER ]] || TPCC_USER=$DB_USER
[[ -n $TPCC_PASS ]] || TPCC_PASS=$DB_PASS
[[ -z $SSL_CA || -r $SSL_CA ]] || die "--ssl-ca: cannot read $SSL_CA"

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

if (( DRY_RUN )); then
    command -v psql >/dev/null || echo "$SCRIPT_NAME: WARN: psql not found (dry run continues)" >&2
else
    command -v psql >/dev/null || die "psql not found - install the postgresql-client package"
fi
if (( RUN_SYSBENCH )); then
    command -v sysbench >/dev/null || die "sysbench not found"
    # The pgsql driver is a compile-time option; without it --db-driver=pgsql
    # only fails once the first run is already under way.
    # (not a pipeline: grep -q exits early, and with pipefail sysbench's SIGPIPE
    # would then look like a missing driver)
    if [[ $(sysbench --help 2>&1) != *pgsql* ]]; then
        die "this sysbench was built without the pgsql driver"
    fi
fi
if (( RUN_HAMMERDB )); then
    [[ -x $HDB_HOME/hammerdbcli ]] || die "hammerdbcli not found in $HDB_HOME"
fi

# ------------------------------------------------------- private temp dir ----
umask 077
TMPSEC=$(mktemp -d "${TMPDIR:-/tmp}/cloud-pg-perf.XXXXXX")
SAMPLER_PID=""

cleanup() {
    [[ -n $SAMPLER_PID ]] && kill "$SAMPLER_PID" 2>/dev/null || true
    rm -rf "$TMPSEC"
}
trap cleanup EXIT INT TERM

# libpq reads the password from PGPASSFILE, so it never reaches the command
# line. ':' and '\' are the only characters that need escaping in that file.
PGPASS="$TMPSEC/pgpass"
pgpass_escape() { local v=${1//\\/\\\\}; printf '%s' "${v//:/\\:}"; }
{
    printf '%s:%s:*:%s:%s\n' "$(pgpass_escape "$DB_HOST")" "$DB_PORT" \
           "$(pgpass_escape "$DB_USER")" "$(pgpass_escape "$DB_PASS")"
    if [[ $TPCC_USER != "$DB_USER" ]]; then
        printf '%s:%s:*:%s:%s\n' "$(pgpass_escape "$DB_HOST")" "$DB_PORT" \
               "$(pgpass_escape "$TPCC_USER")" "$(pgpass_escape "$TPCC_PASS")"
    fi
} >"$PGPASS"
chmod 600 "$PGPASS"

PG_SSLMODE="prefer"
if (( USE_SSL )); then
    PG_SSLMODE="require"
    if [[ -n $SSL_CA ]]; then PG_SSLMODE="verify-ca"; fi
fi

# These are inherited by psql and by sysbench (which links libpq), so TLS and
# the password file apply to both without any per-tool option.
export PGPASSFILE="$PGPASS"
export PGHOST="$DB_HOST" PGPORT="$DB_PORT" PGUSER="$DB_USER"
export PGSSLMODE="$PG_SSLMODE"
export PGCONNECT_TIMEOUT=10
export PGAPPNAME="cloud-pg-perf"
if [[ -n $SSL_CA ]]; then export PGSSLROOTCERT="$SSL_CA"; fi
unset PGPASSWORD PGDATABASE PGOPTIONS 2>/dev/null || true

# sysbench's option-file parser treats '#' as a comment, so values are quoted.
SB_CNF="$TMPSEC/sysbench.cnf"
{
    echo "[sysbench]"
    echo "db-driver=pgsql"
    echo "pgsql-host=$DB_HOST"
    echo "pgsql-port=$DB_PORT"
    echo "pgsql-user=\"$DB_USER\""
    echo "pgsql-password=\"$DB_PASS\""
    echo "pgsql-db=\"$SB_DB\""
} >"$SB_CNF"
if [[ $DB_PASS == *'"'* || $DB_PASS == *'\'* ]]; then
    echo "$SCRIPT_NAME: WARN: sysbench may mis-read a password containing a quote or backslash" >&2
fi
if (( RUN_HAMMERDB )) && [[ $DB_PASS == *'{'* || $DB_PASS == *'}'* || $TPCC_PASS == *'{'* || $TPCC_PASS == *'}'* ]]; then
    echo "$SCRIPT_NAME: WARN: a password containing braces breaks the generated Tcl script" >&2
fi

# $1 = SQL, $2 = database (default: maintenance database). Tab separated, no
# headers - the shape the delta/sampler code expects.
psql_q() {
    psql --dbname="${2:-$MAINT_DB}" -X -q -A -t -F $'\t' -P pager=off -c "$1"
}
# Same, but aligned and with headers, for human-readable captures.
psql_qv() {
    psql --dbname="${2:-$MAINT_DB}" -X -q -P pager=off -c "$1"
}
# Fails the script on the first SQL error - used for CREATE/DROP DATABASE.
psql_strict() {
    psql --dbname="${2:-$MAINT_DB}" -X -q -v ON_ERROR_STOP=1 -P pager=off -c "$1"
}

# Filter for anything HammerDB writes to a log. 'diset' echoes the value it
# sets, and it may echo it Tcl-escaped, so whole lines mentioning a password
# key are dropped in addition to a literal match on either password.
scrub() {
    awk -v pw="$DB_PASS" -v pw2="$TPCC_PASS" '
        { gsub(/\r/,"")
          if ($0 ~ /pg_pass|pg_superuserpass/) { print "<password line redacted>"; next }
          while (pw != "" && (p = index($0, pw)) > 0)
              $0 = substr($0, 1, p-1) "<redacted>" substr($0, p + length(pw))
          while (pw2 != "" && (p = index($0, pw2)) > 0)
              $0 = substr($0, 1, p-1) "<redacted>" substr($0, p + length(pw2))
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
engine            postgresql
started_utc       $(date -u +%Y-%m-%dT%H:%M:%SZ)
client_host       $(hostname)
client_cpus       $(nproc)
client_sysbench   $(command -v sysbench >/dev/null && sysbench --version || echo n/a)
client_psql       $(command -v psql >/dev/null && psql --version || echo n/a)
hammerdb_home     $HDB_HOME
target            $DB_USER@$DB_HOST:$DB_PORT (sslmode=$PG_SSLMODE)
maintenance_db    $MAINT_DB
benchmarks        $BENCHMARKS
runs              $RUNS
threads           $THREAD_LIST
rampup_min        $RAMPUP_MIN
duration_min      $DURATION_MIN
sysbench_schema   $SB_DB, $SB_TABLES tables x {${SB_SIZES[*]}} rows per table
hammerdb_schema   $HDB_DB, {${HDB_WAREHOUSES[*]}} warehouses, allwarehouse=$HDB_ALLWAREHOUSE
hammerdb_procs    stored_procs=$HDB_STORED_PROCS, schema owner=$TPCC_USER
configurations    $total_cfg (sysbench $sb_cfg, hammerdb $hdb_cfg)
schema_rebuild    $rebuild_txt
vacuum_after_load $( (( VACUUM_AFTER_LOAD )) && echo "VACUUM ANALYZE" || echo "off (--no-vacuum)" )
status_sampling   $( (( SAMPLE_INTERVAL > 0 )) && echo "every ${SAMPLE_INTERVAL}s" || echo off )
detail_stats      $( (( DETAIL_STATS )) && echo "before/after every run" || echo "off (--detail-stats to enable)" )
estimated_runtime ${total_min} min (~$(( total_min / 60 ))h, excluding $rebuilds schema rebuilds)
EOF
cat "$OUT/${PFX}_config.txt"

if (( ! DRY_RUN && ! ASSUME_YES && ! PREPARE_ONLY )); then
    printf '\nStarting in 10s - Ctrl-C to abort.\n'
    read -r -t 10 -n 1 _ || true
fi

# --------------------------------------------------- server state capture ----
# Cumulative counters, as 'name<TAB>value' pairs. to_jsonb()/jsonb_each_text()
# melts each pg_stat_* row into pairs, which keeps this working across the
# versions that keep moving columns between those views.
#
# One statement per element: psql runs each -c separately, so a view that does
# not exist on this server (pg_stat_wal before 14, pg_stat_io before 16,
# pg_stat_checkpointer before 17) costs one error line instead of the whole
# snapshot - which is what would happen if they shared a query string.
PG_STATUS_SQL=(
"SELECT 'db.'||d.datname||'.'||j.key, j.value
   FROM pg_stat_database d, jsonb_each_text(to_jsonb(d)) j
  WHERE d.datname IN ('$SB_DB','$HDB_DB','$MAINT_DB') AND j.key <> 'datname'"
"SELECT 'bgwriter.'||j.key, j.value
   FROM pg_stat_bgwriter b, jsonb_each_text(to_jsonb(b)) j"
"SELECT 'wal.'||j.key, j.value
   FROM pg_stat_wal w, jsonb_each_text(to_jsonb(w)) j"
"SELECT 'checkpointer.'||j.key, j.value
   FROM pg_stat_checkpointer c, jsonb_each_text(to_jsonb(c)) j"
"SELECT 'io.'||coalesce(i.backend_type,'all')||'.'||coalesce(i.object,'all')
        ||'.'||coalesce(i.context,'all')||'.'||j.key, j.value
   FROM pg_stat_io i, jsonb_each_text(to_jsonb(i)) j
  WHERE j.key NOT IN ('backend_type','object','context')"
"SELECT 'activity.'||coalesce(state,'unknown'), count(*)::text
   FROM pg_stat_activity GROUP BY 1"
)

# Drops the statements whose view this server does not have, so that a run does
# not log the same "relation does not exist" error a few hundred times.
prune_status_sql() {
    local kept=() sql view
    for sql in "${PG_STATUS_SQL[@]}"; do
        view=$(sed -n 's/.*FROM \(pg_stat_[a-z_]*\).*/\1/p' <<<"$sql" | head -1)
        if [[ -n $view && $(psql_q "SELECT 1 FROM pg_class WHERE relname = '$view'" 2>/dev/null) != 1 ]]; then
            log "note: $view is not available on this server - not collected"
            continue
        fi
        kept+=("$sql")
    done
    PG_STATUS_SQL=("${kept[@]}")
}

# $1 = phase tag, e.g. "baseline" or "sysbench_r1_s10M_t4"
collect_status() {
    local tag=$1 sql
    local out="$OUT/server/${PFX}_status_$tag.tsv"
    : >"$out"
    for sql in "${PG_STATUS_SQL[@]}"; do
        psql_q "$sql" >>"$out" 2>>"$OUT/server/${PFX}_errors.log" || true
    done
}

collect_variables() {
    local tag=$1
    psql_q "SELECT name, setting FROM pg_settings ORDER BY name" \
        >"$OUT/server/${PFX}_variables_$tag.tsv" 2>>"$OUT/server/${PFX}_errors.log" || true
}

db_exists() {
    [[ $(psql_q "SELECT 1 FROM pg_database WHERE datname = '$1'" 2>/dev/null) == 1 ]]
}

# Per-table statistics live in the benchmark database itself, which only exists
# once its data set has been loaded.
table_stats() {
    local db=$1
    echo "-- table statistics ($db)"
    if db_exists "$db"; then
        psql_qv "SELECT relname, n_live_tup, n_dead_tup, n_tup_ins, n_tup_upd, n_tup_del,
                        seq_scan, idx_scan, last_autovacuum, last_autoanalyze
                   FROM pg_stat_user_tables ORDER BY n_dead_tup DESC" "$db" || true
    else
        echo "(database does not exist)"
    fi
}

# Off by default: the activity/lock snapshots are large and only interesting
# when a run misbehaves. Enable with --detail-stats.
collect_detail() {
    local tag=$1
    (( DETAIL_STATS )) || return 0
    {
        echo "-- pg_stat_activity"
        psql_qv "SELECT pid, datname, usename, state, wait_event_type, wait_event,
                        now()-xact_start AS xact_age, left(query,120) AS query
                   FROM pg_stat_activity WHERE backend_type='client backend'
                  ORDER BY xact_age DESC NULLS LAST" || true
        echo "-- pg_locks (not granted first)"
        psql_qv "SELECT l.granted, l.locktype, l.mode, count(*) AS locks
                   FROM pg_locks l GROUP BY 1,2,3 ORDER BY l.granted, locks DESC" || true
        table_stats "$SB_DB"
        table_stats "$HDB_DB"
    } >"$OUT/server/${PFX}_pgstats_$tag.txt" 2>>"$OUT/server/${PFX}_errors.log" || \
        log "WARN: detail statistics failed for $tag"
}

# $1 = optional tag, so the loaded size of every data set can be recorded
collect_server_info() {
    local tag=${1:-} db
    {
        echo "-- version"
        psql_qv "SELECT version() AS version, current_setting('server_version_num') AS version_num,
                        current_setting('server_encoding') AS encoding"
        echo "-- sizing"
        psql_qv "SELECT name, setting, unit FROM pg_settings
                  WHERE name IN ('shared_buffers','effective_cache_size','work_mem',
                                 'maintenance_work_mem','max_connections','max_wal_size',
                                 'min_wal_size','wal_level','wal_compression','synchronous_commit',
                                 'checkpoint_timeout','checkpoint_completion_target',
                                 'random_page_cost','seq_page_cost','autovacuum',
                                 'autovacuum_max_workers','max_worker_processes',
                                 'max_parallel_workers_per_gather','default_transaction_isolation',
                                 'full_page_writes','fsync','huge_pages','shared_preload_libraries')
                  ORDER BY name"
        echo "-- database sizes"
        psql_qv "SELECT datname, pg_size_pretty(pg_database_size(datname)) AS size,
                        pg_database_size(datname) AS bytes
                   FROM pg_database WHERE datname IN ('$SB_DB','$HDB_DB') ORDER BY datname"
        for db in "$SB_DB" "$HDB_DB"; do
            echo "-- largest relations in $db"
            if db_exists "$db"; then
                psql_qv "SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS total
                           FROM pg_catalog.pg_statio_user_tables
                          ORDER BY pg_total_relation_size(relid) DESC LIMIT 10" "$db" || true
            else
                echo "(database does not exist)"
            fi
        done
    } >"$OUT/server/${PFX}_server_info${tag:+_$tag}.txt" 2>>"$OUT/server/${PFX}_errors.log" || true
}

# before-file after-file -> delta of every numeric counter that moved.
# PostgreSQL counters include doubles (blk_read_time, ...), hence the decimals.
status_delta() {
    awk -F'\t' '
        function isnum(x) { return x ~ /^-?[0-9]+(\.[0-9]+)?$/ }
        NR==FNR { before[$1]=$2; next }
        ($1 in before) && isnum(before[$1]) && isnum($2) {
            d = $2 - before[$1]
            if (d != 0) printf "%s\t%s\t%s\t%s\n", $1, before[$1], $2, d
        }' "$1" "$2" | sort
}

start_sampler() {
    local out=$1
    (( SAMPLE_INTERVAL > 0 )) || return 0
    printf 'ts\tvariable\tvalue\n' >"$out"
    local sql="
        SELECT 'db.'||d.datname||'.'||j.key, j.value
          FROM pg_stat_database d, jsonb_each_text(to_jsonb(d)) j
         WHERE d.datname IN ('$SB_DB','$HDB_DB')
           AND j.key IN ('numbackends','xact_commit','xact_rollback','blks_read','blks_hit',
                         'tup_returned','tup_fetched','tup_inserted','tup_updated','tup_deleted',
                         'conflicts','deadlocks','temp_files','temp_bytes',
                         'blk_read_time','blk_write_time')
        UNION ALL
        SELECT 'activity.'||coalesce(state,'unknown'), count(*)::text
          FROM pg_stat_activity WHERE backend_type='client backend' GROUP BY 1"
    (
        while :; do
            ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
            { psql_q "$sql" 2>/dev/null || true; } | awk -F'\t' -v ts="$ts" \
                'NF==2 { print ts "\t" $1 "\t" $2 }' >>"$out"
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
    collect_detail "${tag}_before"
    start_sampler "$dir/${PFX}_samples_$tag.tsv"
    local rc=0
    "$@" || rc=$?
    stop_sampler
    collect_status "${tag}_after"
    collect_detail "${tag}_after"
    status_delta "$dir/${PFX}_status_${tag}_before.tsv" "$dir/${PFX}_status_${tag}_after.tsv" \
        >"$dir/${PFX}_status_delta_$tag.tsv" 2>/dev/null || true
    return $rc
}

# ------------------------------------------------------------- database ------
# DROP DATABASE needs every other session gone; FORCE does that in one step on
# PostgreSQL 13+, and the fallback terminates the backends first.
drop_database() {
    local db=$1
    if psql_strict "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE)" 2>>"$OUT/server/${PFX}_errors.log"; then
        return 0
    fi
    psql_q "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
             WHERE datname = '$db' AND pid <> pg_backend_pid()" \
        >/dev/null 2>>"$OUT/server/${PFX}_errors.log" || true
    psql_strict "DROP DATABASE IF EXISTS \"$db\"" 2>>"$OUT/server/${PFX}_errors.log"
}

create_database() {
    local db=$1 owner=$2
    psql_strict "CREATE DATABASE \"$db\" OWNER \"$owner\"" 2>>"$OUT/server/${PFX}_errors.log"
}

# ------------------------------------------------------------- sysbench ------
SB_CSV="$OUT/${PFX}_sysbench_results.csv"
# --table-size varies per data set, so it is added by each function.
sb_common=(--config-file="$SB_CNF" --tables="$SB_TABLES")

# Drops the sysbench data set. Dropping the whole database is the cheapest and
# the most thorough way, but not every managed service grants CREATEDB, so fall
# back to the tool's own cleanup (which only knows about the current --tables).
# $1 = rows per table, $2 = tag used in the log file name
sysbench_drop() {
    local rows=$1 tag=$2
    if (( DRY_RUN )); then return 0; fi
    if drop_database "$SB_DB" && create_database "$SB_DB" "$DB_USER"; then return 0; fi
    log "WARN: cannot recreate database $SB_DB - falling back to 'sysbench cleanup'"
    sysbench "${sb_common[@]}" --table-size="$rows" oltp_read_write cleanup \
        >>"$OUT/sysbench/${PFX}_cleanup_$tag.log" 2>&1 || true
}

# Rebuilds the data set from scratch; $1 = rows per table, $2 = file name tag.
sysbench_prepare() {
    local rows=$1 tag=$2
    hdr "SYSBENCH PREPARE $tag ($SB_TABLES tables x $rows rows)"
    local cmd=(sysbench "${sb_common[@]}" --table-size="$rows" --threads="$SB_PREPARE_THREADS" oltp_read_write prepare)
    log "recreating $SB_DB and running sysbench oltp_read_write prepare ($SB_TABLES x $rows rows, threads=$SB_PREPARE_THREADS)"
    if (( DRY_RUN )); then printf '  %s\n' "${cmd[*]}" | sed 's/--config-file=[^ ]*/--config-file=<creds>/'; return 0; fi
    sysbench_drop "$rows" "$tag"
    "${cmd[@]}" >"$OUT/sysbench/${PFX}_prepare_$tag.log" 2>&1 \
        || { tail -20 "$OUT/sysbench/${PFX}_prepare_$tag.log"; die "sysbench prepare failed"; }
    # A freshly loaded table has no visibility map and no statistics, and the
    # planner would pick different plans for the first and the later runs.
    if (( VACUUM_AFTER_LOAD )); then
        log "VACUUM ANALYZE $SB_DB"
        psql_q "VACUUM (ANALYZE)" "$SB_DB" >>"$OUT/sysbench/${PFX}_prepare_$tag.log" 2>&1 \
            || log "WARN: VACUUM ANALYZE on $SB_DB failed"
    fi
    log "prepare done ($(psql_q "SELECT pg_size_pretty(pg_database_size('$SB_DB'))" 2>/dev/null || echo '?'))"
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
    if (( DRY_RUN )); then return 0; fi
    drop_database "$SB_DB" || log "WARN: could not drop $SB_DB"
}

# ------------------------------------------------------------- HammerDB ------
HDB_CSV="$OUT/${PFX}_hammerdb_results.csv"
# HammerDB persists its settings dictionary (including the passwords) and its
# jobs database under $TMP, so $TMP stays in the private temp dir and only
# scrubbed text logs are copied into the results directory.
export TMP="$TMPSEC/hammerdb-tmp"
mkdir -p "$TMP"

hdb_conn_block() {
    cat <<EOF
dbset db pg
dbset bm TPC-C
diset connection pg_host $DB_HOST
diset connection pg_port $DB_PORT
diset connection pg_sslmode $PG_SSLMODE
diset tpcc pg_superuser {$DB_USER}
diset tpcc pg_superuserpass {$DB_PASS}
diset tpcc pg_defaultdbase $MAINT_DB
diset tpcc pg_user {$TPCC_USER}
diset tpcc pg_pass {$TPCC_PASS}
diset tpcc pg_dbase $HDB_DB
EOF
}

# Runs a generated tcl script; $1 = script path, $2 = log path.
# 'diset' echoes every value it sets, so password lines are redacted on the way
# into the log. pipefail propagates hammerdbcli's exit status.
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

# Drops the TPC-C database; $1 = tag used in the log file name. 'deleteschema'
# is only the fallback - DROP DATABASE is faster and also copes with a schema
# that a failed build left half-finished.
hammerdb_drop() {
    local tag=$1
    if (( DRY_RUN )); then return 0; fi
    if drop_database "$HDB_DB"; then return 0; fi
    log "WARN: cannot drop database $HDB_DB - falling back to HammerDB deleteschema"
    local tcl="$TMPSEC/drop.tcl"
    { hdb_conn_block; echo "deleteschema"; } >"$tcl"
    hdb_cli "$tcl" "$OUT/hammerdb/${PFX}_deleteschema_$tag.log" || true
}

# Rebuilds the TPC-C schema from scratch; $1 = warehouses, $2 = file name tag.
# HammerDB creates the database itself, so this only drops beforehand.
hammerdb_build() {
    local wh=$1 tag=$2
    hdr "HAMMERDB SCHEMA BUILD $tag ($wh warehouses)"
    local tcl="$TMPSEC/build.tcl"
    local build_vu=$(( HDB_BUILD_VU < wh ? HDB_BUILD_VU : wh ))
    {
        hdb_conn_block
        cat <<EOF
diset tpcc pg_count_ware $wh
diset tpcc pg_num_vu $build_vu
diset tpcc pg_tspace pg_default
diset tpcc pg_partition false
diset tpcc pg_storedprocs $HDB_STORED_PROCS
diset tpcc pg_vacuum $( (( VACUUM_AFTER_LOAD )) && echo true || echo false )
buildschema
EOF
    } >"$tcl"
    sed -E "s/^diset tpcc (pg_pass|pg_superuserpass) .*/diset tpcc \1 <redacted>/" "$tcl" \
        >"$OUT/hammerdb/${PFX}_build_$tag.tcl"
    log "dropping $HDB_DB and building $wh warehouses with $build_vu virtual users (stored_procs=$HDB_STORED_PROCS)"
    if (( DRY_RUN )); then sed -E 's/(pg_pass|pg_superuserpass) .*/\1 <redacted>/' "$tcl"; return 0; fi
    hammerdb_drop "$tag"
    local log_file="$OUT/hammerdb/${PFX}_build_$tag.log"
    hdb_cli "$tcl" "$log_file" \
        || { hdb_save_tmp_logs "build_$tag"; tail -30 "$log_file"; die "HammerDB schema build failed"; }
    hdb_save_tmp_logs "build_$tag"
    grep -qi "SCHEMA COMPLETE\|Schema Complete" "$log_file" \
        || log "WARN: 'schema complete' not found in ${log_file##*/} - check it before trusting results"
    log "schema build done ($(psql_q "SELECT pg_size_pretty(pg_database_size('$HDB_DB'))" 2>/dev/null || echo '?'))"
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
diset tpcc pg_count_ware $wh
diset tpcc pg_driver timed
diset tpcc pg_rampup $RAMPUP_MIN
diset tpcc pg_duration $DURATION_MIN
diset tpcc pg_allwarehouse $HDB_ALLWAREHOUSE
diset tpcc pg_timeprofile true
diset tpcc pg_raiseerror true
diset tpcc pg_keyandthink false
diset tpcc pg_storedprocs $HDB_STORED_PROCS
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
    sed -E "s/^diset tpcc (pg_pass|pg_superuserpass) .*/diset tpcc \1 <redacted>/" "$tcl" \
        >"$OUT/hammerdb/${PFX}_$name.tcl"
    if (( DRY_RUN )); then printf '  hammerdbcli auto %s.tcl (%s warehouses, rampup %s min, duration %s min)\n' "$name" "$wh" "$RAMPUP_MIN" "$DURATION_MIN"; return 0; fi
    hdb_cli "$tcl" "$log" || log "WARN: hammerdbcli run $run wh=$wh vu=$vu exited non-zero"
    hdb_save_tmp_logs "$name"
    hammerdb_parse "$run" "$wh" "$vu" "$log" >>"$HDB_CSV"
}

hammerdb_parse() {
    local run=$1 wh=$2 vu=$3 file=$4
    # "System achieved 12345 NOPM from 34567 PostgreSQL TPM"
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

# ----------------------------------------------------------- preflight ----
hdr "PREFLIGHT"
if (( DRY_RUN )); then
    log "dry run: skipping connection check"
else
    psql_q 'SELECT 1' >/dev/null || die "cannot connect to $DB_HOST:$DB_PORT as $DB_USER (db $MAINT_DB)"
    log "connected to $DB_HOST:$DB_PORT, $(psql_q 'SELECT version()' | head -1)"
    log "ssl: sslmode=$PG_SSLMODE, $(psql_q "SELECT ssl, coalesce(version,'-'), coalesce(cipher,'-') FROM pg_stat_ssl WHERE pid = pg_backend_pid()" 2>/dev/null | tr '\t' ' ' || echo unknown)"
    if [[ $REBUILD != never ]] && ! psql_q "SELECT rolcreatedb FROM pg_roles WHERE rolname=current_user" | grep -q t; then
        log "WARN: $DB_USER has no CREATEDB - schema rebuilds will fall back to in-place cleanup"
    fi
    prune_status_sql
    collect_variables baseline
    collect_status baseline
    collect_detail baseline
fi

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
    collect_detail final
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
