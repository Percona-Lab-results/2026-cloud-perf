# 2026-cloud-perf
Performance research of standard managed DB tiers in minor cloud providers

## cloud-mysql-perf.sh

OLTP benchmark driver for a managed MySQL endpoint. Runs both benchmarks over a
thread matrix, repeats the whole matrix N times, and collects the server-side
state around every run.

| | sysbench | HammerDB |
|---|---|---|
| workload | `oltp_read_write` | TPC-C (TPROC-C) |
| data set | 10 tables x 1,000,000 rows (~2.5 GB) | 25 warehouses (~2.5 GB) |
| load levels | 1, 2, 4, 8 threads | 1, 2, 4, 8 virtual users |
| ramp-up | 5 min, discarded | 5 min (`mysql_rampup`) |
| measured | 20 min | 20 min (`mysql_duration`) |
| metric | tps / qps / latency incl. p95 | NOPM / TPM |

Defaults: 3 runs of the matrix = 24 configurations ~ 10 h. Runs form the outer
loop, so an interrupted session still contains complete passes over the matrix.

### Usage

`--provider`, `--host` and `--user` are mandatory, as is a password
(`--password`, `--password-file` or `MYSQL_PWD`).

```bash
./cloud-mysql-perf.sh --provider digitalocean \
    -H db.example.com -P 25060 -u doadmin -p 'secret' --label 2vcpu-4gb

./cloud-mysql-perf.sh --provider linode -H db.example.com -u admin \
    --password-file ~/.pw --runs 1 -t "1 8" -b sysbench   # quick single pass
./cloud-mysql-perf.sh --provider hetzner -H ... -n        # dry run, print plan
./cloud-mysql-perf.sh --help                              # all options
```

Useful flags: `--runs`, `--threads`, `--rampup`, `--duration`, `--benchmarks`,
`--skip-prepare` (reuse existing data sets), `--prepare-only`,
`--cleanup-after`, `--ssl`, `--sample-interval`.

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
  <provider>_summary.txt            per-thread-level means
  <provider>_run.log
  <provider>_sysbench_results.csv   provider,benchmark,run,threads,tps,qps,
                                    lat_min/avg/p95/max,errors
  <provider>_hammerdb_results.csv   provider,benchmark,run,vu,nopm,tpm,status
  sysbench/                         full sysbench output per run (10s reports)
  hammerdb/                         per-run tcl + hammerdbcli log, VU log, xtprof
  server/
    <provider>_variables_{baseline,final}.tsv
    <provider>_status_<phase>.tsv         SHOW GLOBAL STATUS before/after each run
    <provider>_status_delta_<phase>.tsv   counters that moved during the run
    <provider>_samples_<phase>.tsv        key counters every --sample-interval sec
    <provider>_innodb_status_<phase>.txt  SHOW ENGINE INNODB STATUS before/after
    <provider>_server_info.txt            version, buffer pool, isolation, sizes
```

Comparing providers is then just:

```bash
awk 'FNR>1 || NR==1' results/*/*_sysbench_results.csv > all_sysbench.csv
```

### Notes for managed services

* The benchmark user needs `CREATE` on both schemas; `PROCESS` is needed for
  `SHOW ENGINE INNODB STATUS` (the script warns and continues without it).
* HammerDB creates stored procedures by default. With binary logging on and
  `log_bin_trust_function_creators=OFF` that fails without `SUPER`; in that case
  build the schema with `--no-stored-procs yes`, or let the server decide with
  `--no-stored-procs auto`.
* HammerDB connects over the unix socket whenever the host is
  `localhost`/`127.0.0.1` - use the routable address, or pass `--socket`.
* sysbench's measured window is produced with `--report-checkpoints`, which
  resets the counters at the end of the ramp-up; the connections are never
  dropped, so the 25 minutes are one continuous run.
* Run the client close to the database (same region) - otherwise round-trip
  time, not the database tier, is what gets measured.
