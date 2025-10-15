# DB2 ST_Transform Bug Reproduction

This repository contains a self-contained harness that launches the IBM DB2 Community Edition container, spatially enables the bundled SAMPLE database, and runs a multithreaded Python workload that repeatedly executes the problematic `ST_Transform` query until DB2 fails.

## Prerequisites

- Docker CLI with access to pull `icr.io/db2_community/db2`
  - On macOS, ensure Docker Desktop supports `linux/amd64`; the harness will automatically set the platform flag.
- Python 3.9+ and `make`

## Quick Start

```bash
make test
```

The `test` target will

1. Provision a virtual environment and install `ibm-db`.
2. Start a fresh DB2 container (`db2-st-transform`) with the SAMPLE database.
3. Install the spatial extensions on SAMPLE.
4. Launch the threaded query hammer.

The hammer runs for 5 minutes by default. If no failure is observed, re-run with a larger duration or let it run indefinitely:

```bash
make test ARGS="--duration 0"
```

## Useful Options

You can pass additional arguments to the harness via the `ARGS` variable:

```bash
make test ARGS="--threads 16 --pool-size 24 --log-level DEBUG"
```

Run `python -m scripts.repro_runner --help` for the full list of supported options, including overriding the image tag, reusing an existing container, and keeping the container alive for post-mortem inspection.

## Target SQL

The workload repeatedly issues the following statement (see `scripts/repro_runner.py:11`):

```sql
SELECT CASE
           WHEN DB2GSE.ST_IsEmpty(DB2GSE.ST_Point(CAST(-98.71447796 AS DOUBLE),
                                                  CAST(29.48604692 AS DOUBLE), CAST(4269 AS INTEGER)))=1
                OR DB2GSE.ST_IsValid(DB2GSE.ST_Point(CAST(-98.71447796 AS DOUBLE),
                                                     CAST(29.48604692 AS DOUBLE), CAST(4269 AS INTEGER)))=0
           THEN NULL
           ELSE CAST(db2gse.ST_AsText(db2gse.ST_Transform(DB2GSE.ST_Point(CAST(-98.71447796 AS DOUBLE),
                                                                          CAST(29.48604692 AS DOUBLE), CAST(4269 AS INTEGER)), CAST(4326 AS INTEGER))) AS CLOB(2097152))
      END
FROM SYSIBM.SYSDUMMY1
```

The query validates a single NAD83 point and transforms it into WGS84 (`ST_Transform(..., 4326)`), returning the result as WKT. When this runs concurrently across many connections, DB2’s spatial extender intermittently hits the `sqlzAssertFailed` path captured in the diagnostic log.

## Reproducing And Confirming The Bug

1. Start the test harness (runs for five minutes by default):
   ```bash
   make test
   ```
   The container init phase takes several minutes while DB2 creates the SAMPLE database and the harness spatially enables it using `db2se enable_db SAMPLE`.

2. Watch the DB2 diagnostic output in real time:
   ```bash
   docker logs -f db2-st-transform
   ```

3. A successful reproduction emits a non-fatal assertion followed by an `ADM14005E` message similar to:
   ```
   2025-10-14-23.25.10.086038+000 I363420E1996          LEVEL: Severe
   PID     : 52263                TID : 46913160209984  PROC : db2sysc 0
   INSTANCE: db2inst1             NODE : 000            DB   : SAMPLE
   APPHDL  : 0-49                 APPID: 127.0.0.1.65134.251014232507
   ...
   NON-FATAL ASSERTION FAILED!!!
   ASSERTION EXPRESSION: Invalid pad type (0x2AAC) found at:
   SOURCE FILENAME: /supp/oemtools/ALL/spatial_esri/base/db2/gseOss.cpp
   ...
   ADM14005E  The following error occurred: "AppErr".  First Occurrence Data Capture (FODC) has been invoked in the following mode: "Automatic".
   ```
   The same details are stored inside the container at `/database/config/db2inst1/sqllib/db2dump/db2diag.log` and the corresponding FODC directory (for example, `.../FODC_AppErr_2025-10-14-23.25.11.895779_52263_22_000/`).

4. When allowing the harness to run indefinitely (`make test ARGS="--duration 0"`), restart DB2 after the crash or re-run `make test` (the harness tears down the container automatically).

## Observed Failure Pattern

The behaviour is consistently reproducible on a fresh SAMPLE database with default configuration values.

**Reproduction summary**

- `make test ARGS="--threads 1 --pool-size 1 --duration 30"` completes without errors (≈444 iterations in 30 s). 
- `make test ARGS="--threads 2 --pool-size 2 --duration 300"` fails after ~7 s/≈200 iterations with:
  ```
  SQL0430N  User defined function "DB2GSE.GSETRANSFORM" (specific name "GSETRANSFORM") has abnormally terminated.  SQLSTATE=38503 SQLCODE=-430
  SQL0443N  Routine "*RANSFORM" ... diagnostic text "GSE3015N  Reason code = "-2901".  Transformation to SRS "4".  SQLSTATE=38SUC
  ```
  These errors also appear in the container console (`docker logs -f db2-st-transform`) and in `db2diag.log` inside the container.

**Diagnostic log excerpts**

`/database/config/db2inst1/sqllib/db2dump/DIAG0000/db2diag.log` captures a repeating sequence of non-fatal assertions from the ESRI spatial library immediately before DB2 terminates the UDF:

```
2025-10-15-01.02.55.529891+000 I430009E2008 LEVEL: Severe
ASSERTION EXPRESSION: Invalid block eye-catcher (0xDEAD055E) found at:
SOURCE FILENAME: /supp/oemtools/ALL/spatial_esri/base/db2/gseOss.cpp
CALLSTCK:
  ... pe_factory_xtlist_cache_unload
  pe_factory_xtlist_cache_uninit
  pe_factory_uninit
  SgShapeChangeCoordRef_with_geotran
  gseTransformCS
  sqloInvokeUDF
  sqlriFetch
```

Additional assertions in the same burst show:

- `Invalid pad type (0x2AAD)`
- `Invalid pad type (0x95A667EB)`
- `Freeing freed memory found at:`

Seconds later, DB2 logs `ADM14005E` (“An unfenced User Defined Function (UDF) was abnormally terminated… It is recommended that DB2 server instance is stopped and restarted as soon as possible.”) and triggers First Occurrence Data Capture.

**FODC / trap evidence**

- The crash bundle lives under `/database/config/db2inst1/sqllib/db2dump/DIAG0000/FODC_AppErr_<timestamp>_<pid>_<eduid>_000/` (example: `FODC_AppErr_2025-10-15-00.09.56.099839_52256_149_000/`).
- `52256.149.000.trap.txt` inside that directory shows the engine received SIGSEGV at address `0x00000200AABBCCDD`. DB2 uses `0xAABBCCDD` as the sentinel for “already freed” memory, proving a double free.
- The captured stack trace matches the diagnostic log: `pe_database_uninit → pe_factory_xtlist_cache_unload → pe_factory_xtlist_cache_uninit → SgShapeChangeCoordRef_with_geotran → gseTransformCS → sqloInvokeUDF`.

**Interpretation**

- `DB2GSE.ST_Transform` is deployed as an unfenced UDF (check `syscat.functions`). An unfenced failure propagates into the db2sysc process.
- The ESRI spatial extender maintains shared state in the `pe_factory` cache. When multiple agents call `ST_Transform` concurrently, two threads tear down the same cache. The second free trips the OSS heap guard (`Invalid block eye-catcher`, `Invalid pad type`, `Freeing freed memory`).
- No DB2 configuration parameter or environment tweak is involved—the SAMPLE database runs with default CFG values (`db2 get db cfg for SAMPLE`). The issue is a thread-safety bug in the spatial extender (`gseOss.cpp`), not an application misuse.
- Until IBM delivers a fix, the only safe workaround is to serialize ST_Transform calls (single worker / connection). Any higher concurrency eventually produces the GSE3015N/SQL0430N errors followed by the assertion/segfault sequence and requires a DB2 instance restart.

## Cleanup

```bash
make container-stop   # stop and remove just the container
make clean            # remove the container and the virtual environment
```
