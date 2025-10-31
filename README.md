# DB2 ST_Transform Bug Reproduction

This repository contains a self-contained harness that launches the IBM DB2 Community Edition container, spatially enables the bundled SAMPLE database, and runs a multithreaded Python workload that repeatedly executes the problematic `ST_Transform` query until DB2 fails.

## Prerequisites

- Docker CLI with access to pull `icr.io/db2_community/db2`
  - On macOS, ensure Docker Desktop supports `linux/amd64`; the harness will automatically set the platform flag.
- Python 3.9+ and `make`

### Additional Windows Prerequisites

The following tools are required when running on Windows:

1. **Python 3.9+**
   - Download and install from [python.org](https://www.python.org/downloads/)
   - During installation, check "Add Python to PATH"
   - Verify with: `python --version`

2. **GNU Make**
   - Install using [Chocolatey](https://chocolatey.org/): `choco install make`
   - Or download from [GnuWin32](http://gnuwin32.sourceforge.net/packages/make.htm)
   - Verify with: `make --version`

3. **Visual C++ Runtime**
   - Required for the IBM DB2 driver (included in wheel package)
   - If not already installed, download the [Visual C++ Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe)
   - Or install Visual Studio Build Tools if you need to build from source:

4. **Windows Long Paths Support**
   - Required if your workspace is in a deep directory structure (like OneDrive)
   - Enable via Group Policy or Registry:
     ```powershell
     # Run in Administrator PowerShell
     Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name 'LongPathsEnabled' -Value 1
     ```
   - Sign out and sign back in (or restart) after enabling

### Windows-Specific Considerations

When running on Windows, there are a few important considerations:

1. **Path Length Limitations**
   - Windows has a default 260-character path limit
   - If your workspace is in a deep directory (e.g., OneDrive), you might hit this limit
   - The Makefile automatically uses `%TEMP%\.venv-db2bug` for the virtual environment on Windows to avoid path issues
   - Enable Windows long paths support (see prerequisites) if needed

2. **Shell Commands**
   - The Makefile includes Windows-specific commands for cleaning and container management
   - Uses `rmdir /s /q` instead of `rm -rf`
   - Redirects to `nul` instead of `/dev/null`

3. **Virtual Environment Location**
   - Default: `%TEMP%\.venv-db2bug` on Windows (shorter path)
   - Unix/Linux: `.venv` in the project directory
   - This location can be overridden by setting the `VENV` variable:
     ```bash
     make test VENV=path/to/venv
     ```

4. **IBM DB2 Driver Setup**
   - The `ibm-db` wheel package includes the DB2 client library (`clidriver`)
   - The Makefile automatically configures the runtime environment:
     - Uses `scripts/run_with_db2.bat` to set up paths
     - Sets `PATH` to include the clidriver bin directory
     - Sets `IBM_DB_HOME` to the clidriver location
     - Preloads DB2 native DLLs before importing Python modules
   - Always use `make test` to run the harness (not `python -m scripts.repro_runner`)
   
   If you encounter DLL loading issues:
   - Ensure you're using Python 3.9 or later
   - Check that the wheel installed successfully (`pip list | findstr ibm-db`)
   - Verify `clidriver` exists in `%TEMP%\.venv-db2bug\Lib\site-packages\clidriver`
   - If using a custom location, update the `VENV` variable to match

## Quick Start

```powershell
# Windows
make test

# Unix/Linux
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

Additional helpful switches include:

- `--db2level 11.5.0.9` to test against a specific Db2 Community Edition tag (maps to `icr.io/db2_community/db2:11.5.0.9`).
- `--ibmcasenumber TS020534809` to annotate logs for a particular IBM support ticket.

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

## Collecting Support Data

To gather a fresh reproduction together with a `db2support` bundle for IBM, run:

```bash
make support-bundle CASE=TS020534809 LEVEL=11.5.0.9
```

The target reproduces the crash using the configured thread/pool settings, executes
`db2support` with full collection (`-F`) and the relevant FODC symptom (`-fodc AppErr`),
and copies the resulting archive and container logs into
`docs/ibm_case-<case#>-<level>-<timestamp>/` (for example `docs/ibm_case-TS020534809-11.5.0.9-20251015T120000Z/`).

This repository currently contains a captured bundle for case `TS020534809` (collected
prior to the new naming convention) in `docs/ibm_case-TS020534809/`.

## Coordinated Disclosure

A draft vulnerability report (suitable for IBM PSIRT submission and CVE coordination) lives at `docs/CVE-report-draft.md`.  
See `SECURITY.md` for the responsible disclosure policy and IBM contact details.
