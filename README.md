# 2026-cloud-perf
Performance research of standard managed DB tiers in minor cloud providers

Two drivers, same matrix, same output layout, so MySQL and PostgreSQL tiers of
the same provider are directly comparable:

* [cloud-mysql-perf.sh](cloud-mysql-perf.sh) - managed MySQL
* [cloud-pg-perf.sh](cloud-pg-perf.sh) - managed PostgreSQL

## cloud-mysql-perf.sh

OLTP benchmark driver for a managed MySQL endpoint. Sweeps the thread matrix
once per data-set size, repeats everything N times, and collects the
server-side state around every run.

| | sysbench | HammerDB |
|---|---|---|
| workload | `oltp_read_write` | TPC-C (TPROC-C) |
| data sets | 10 tables x 10M / 20M / 40M rows per table | 160 / 320 / 640 warehouses |
| load levels | 1, 2, 4, 8 threads | 1, 2, 4, 8 virtual users |
| ramp-up | 5 min, discarded | 5 min (`mysql_rampup`) |
| measured | 20 min | 20 min (`mysql_duration`) |
| metric | tps / qps / latency incl. p95 | NOPM / TPM |

The data-set size is the outer dimension of each sweep (`--table-size`,
`--warehouses`; both accept a list, `K`/`M` suffixes allowed), so the point of
the exercise - how a tier behaves as the data grows past its buffer pool - is
one column of the CSVs.

Loading granularity is `--rebuild`:

| `--rebuild` | reload | rebuilds with the defaults |
|---|---|---|
| `run` (default) | before every measured run | 72 |
| `dataset` | once per data set and run, then sweep the thread levels | 18 |
| `never` (`--skip-prepare`) | reuse whatever is loaded | 0 |

Defaults: 3 runs x 3 data sets x 4 load levels x 2 benchmarks = 72
configurations ~ 30 h of measured time, and the default `--rebuild run` adds a
full load of up to 400M rows / 640 warehouses before each of them. For a
realistic schedule use `--rebuild dataset`, or cut `--runs` / `--threads`.
Runs form the outer loop, so an interrupted session still contains complete
passes over every data set.

### Usage

`--provider`, `--host` and `--user` are mandatory, as is a password
(`--password`, `--password-file` or `MYSQL_PWD`).

```bash
./cloud-mysql-perf.sh --provider digitalocean \
    -H db.example.com -P 25060 -u doadmin -p 'secret' --label 2vcpu-4gb

./cloud-mysql-perf.sh --provider linode -H db.example.com -u admin \
    --password-file ~/.pw --runs 1 -t "1 8" -b sysbench \
    --table-size 10M --rebuild dataset            # one data set, one pass
./cloud-mysql-perf.sh --provider hetzner -H ... -n # dry run, print the plan
./cloud-mysql-perf.sh --help                       # all options
```

Useful flags: `--runs`, `--threads`, `--rampup`, `--duration`, `--benchmarks`,
`--table-size`, `--warehouses`, `--rebuild`, `--prepare-only`,
`--cleanup-after`, `--ssl`, `--sample-interval`, `--innodb-status`.

Credentials are written to 0600 option files in a private temp directory and
passed to `mysql`/`sysbench` via `--defaults-extra-file` / `--config-file`, so
they never appear in the process list. HammerDB needs the password inside its
Tcl script, so that script is kept in the temp directory and only a redacted
copy plus scrubbed logs are written to the results directory.

### Results layout

The provider names the results directory and prefixes every file inside it, and
it is the first column of both CSVs - so results from several providers can be
dropped into one directory or concatenated without losing their origin.

```
results/<provider>-<UTC timestamp>-<label>/
  <provider>_config.txt             provider, parameters, client versions, estimate
  <provider>_summary.txt            means per data set and load level
  <provider>_run.log
  <provider>_sysbench_results.csv   provider,benchmark,run,tables,
                                    rows_per_table,threads,...,tps,qps,
                                    lat_min/avg/p95/max,errors
  <provider>_hammerdb_results.csv   provider,benchmark,run,warehouses,vu,
                                    nopm,tpm,status
  sysbench/                         full sysbench output + prepare log per run
  hammerdb/                         per-run tcl + hammerdbcli log, VU log,
                                    xtprof, and the schema build log per run
  server/
    <provider>_variables_{baseline,final}.tsv
    <provider>_status_<phase>.tsv         SHOW GLOBAL STATUS before/after each run
    <provider>_status_delta_<phase>.tsv   counters that moved during the run
    <provider>_samples_<phase>.tsv        key counters every --sample-interval sec
    <provider>_innodb_status_<phase>.txt  only with --innodb-status
    <provider>_server_info.txt            version, buffer pool, isolation, sizes
    <provider>_server_info_<dataset>.txt  on-disk size of each loaded data set
```

Phase tags carry the data set, so every artifact is attributable:
`sysbench_r2_s20M_t4`, `hammerdb_r2_w320_vu4`.

Comparing providers is then just:

```bash
awk 'FNR>1 || NR==1' results/*/*_sysbench_results.csv > all_sysbench.csv
```

### Notes for managed services

* The benchmark user needs `CREATE` on both schemas and `DROP` for the rebuild
  between runs; without `DROP DATABASE` the script falls back to
  `sysbench cleanup` / HammerDB `deleteschema`.
* `SHOW ENGINE INNODB STATUS` is not collected unless `--innodb-status` is
  given - it needs `PROCESS`, which managed services often withhold (the script
  warns and continues when it is denied).
* HammerDB creates stored procedures by default. With binary logging on and
  `log_bin_trust_function_creators=OFF` that fails without `SUPER`; in that case
  build the schema with `--no-stored-procs yes`, or let the server decide with
  `--no-stored-procs auto`.
* HammerDB connects over the unix socket whenever the host is
  `localhost`/`127.0.0.1` - use the routable address, or pass `--socket`.
* Schema builds dominate the wall clock at these sizes. `--build-vu` (default
  16, capped at the warehouse count) is what makes a 640-warehouse load
  tolerable; raise it only as far as the tier's `max_connections` allows.
* Check that the tier has the disk space for the largest data set before
  starting - 10x40M sysbench rows is roughly 100 GB, 640 warehouses ~64 GB, and
  both schemas exist side by side.
* sysbench's measured window is produced with `--report-checkpoints`, which
  resets the counters at the end of the ramp-up; the connections are never
  dropped, so the 25 minutes are one continuous run.
* Run the client close to the database (same region) - otherwise round-trip
  time, not the database tier, is what gets measured.

## cloud-pg-perf.sh

The PostgreSQL counterpart: same benchmarks (`sysbench oltp_read_write` through
sysbench's `pgsql` driver, HammerDB TPROC-C with `dbset db pg`), same data sets,
thread levels, ramp-up/duration, `--rebuild` semantics, provider prefixing and
CSV columns, so the two scripts' results can be compared column by column.

```bash
./cloud-pg-perf.sh --provider digitalocean \
    -H db.example.com -P 25060 -u doadmin -p 'secret' \
    --maintenance-db defaultdb --ssl --label 2vcpu-4gb

./cloud-pg-perf.sh --provider linode -H db.example.com -u admin \
    --password-file ~/.pw --runs 1 -t "1 8" -b sysbench \
    --table-size 10M --rebuild dataset
./cloud-pg-perf.sh --help
```

Requirements: `postgresql-client` (for `psql`) and a sysbench built with the
`pgsql` driver - the Ubuntu package is, and `sysbench --help` lists the
compiled-in drivers. HammerDB 6.0 ships its PostgreSQL library inside the
binary, so no extra Tcl package is needed.

What differs from the MySQL script:

| | MySQL | PostgreSQL |
|---|---|---|
| credentials | 0600 `--defaults-extra-file` | 0600 `PGPASSFILE` + `PG*` env vars |
| TLS | `--ssl` | `--ssl` (`sslmode=require`), `--ssl-ca FILE` (`verify-ca`) |
| admin connection | any schema | `--maintenance-db` (default `postgres`, DO uses `defaultdb`) |
| settings | `SHOW GLOBAL VARIABLES` | `pg_settings` |
| counters | `SHOW GLOBAL STATUS` | `pg_stat_database`/`_bgwriter`/`_wal`/`_checkpointer`/`_io`, melted to name/value pairs |
| deep dive | `--innodb-status` | `--detail-stats` (`pg_stat_activity`, `pg_locks`, `pg_stat_user_tables`) |
| stored procedures | `--no-stored-procs` (default no) | `--stored-procs yes\|no` (default no) |
| after a load | - | `VACUUM ANALYZE` (and HammerDB `pg_vacuum`), off with `--no-vacuum` |

Extra notes:

* The benchmark user needs `CREATEDB`; the rebuild uses
  `DROP DATABASE ... WITH (FORCE)` and falls back to terminating the backends,
  then to `sysbench cleanup` / HammerDB `deleteschema`. The preflight warns when
  `CREATEDB` is missing.
* A fresh load has no visibility map and no statistics, so `VACUUM ANALYZE`
  after loading is on by default - without it the first run of a data set
  measures different plans and hint-bit writes rather than the tier.
* Counter views come and went across versions (`pg_stat_wal` 14,
  `pg_stat_io` 16, `pg_stat_checkpointer` 17). The preflight drops the ones this
  server lacks and says so; everything else is collected as name/value pairs, so
  column churn does not break the deltas.
* `--tpcc-user` defaults to the connection user, so no role has to be created on
  a managed service; HammerDB creates the `tpcc` database itself.
* The `errors` column of the sysbench CSV counts ignored errors, which on
  PostgreSQL are mostly serialization/deadlock retries. They are common on small
  data sets with several threads and should be near zero at 10M+ rows - if not,
  the number belongs in the write-up.
* `pg_stat_ssl` is logged at preflight, so the results record whether the run
  was actually encrypted.
