SHELL := /bin/sh

# Use shorter path for virtual environment on Windows to avoid path length issues
ifeq ($(OS),Windows_NT)
	VENV ?= $(TEMP)\.venv-db2bug
else
	VENV ?= .venv
endif

PYTHON ?= python

SUPPORT_THREADS ?= 4
SUPPORT_POOL ?= 4
SUPPORT_DURATION ?= 0
SUPPORT_CONTAINER ?= db2-st-transform
SUPPORT_INSTANCE ?= db2inst1
SUPPORT_PORT ?= 50000
SUPPORT_PASSWORD ?= Password123!
SUPPORT_DATABASE ?= SAMPLE
TRACE_THREADS ?= $(SUPPORT_THREADS)
TRACE_MAX_SECONDS ?= 120
TRACE_FODC_POLL ?= 5
TRACE_FODC_QUIESCE ?= 10
TRACE_FODC_TIMEOUT ?= 300

ifeq ($(OS),Windows_NT)
	VENV_BIN := $(VENV)/Scripts
	PYTHON_BIN := $(VENV_BIN)/python.exe
else
	VENV_BIN := $(VENV)/bin
	PYTHON_BIN := $(VENV_BIN)/python3
endif

.PHONY: install test clean container-stop support-bundle

install: $(PYTHON_BIN)

$(PYTHON_BIN): requirements.txt
	$(PYTHON) -m venv $(VENV)
	$(PYTHON_BIN) -m pip install --upgrade pip
	$(PYTHON_BIN) -m pip install -r requirements.txt

test: install
ifeq ($(OS),Windows_NT)
	@scripts\run_with_db2.bat "$(PYTHON_BIN)" "scripts.repro_runner" "$(VENV)" "$(ARGS)"
else
	$(PYTHON_BIN) -m scripts.repro_runner $(ARGS)
endif

support-bundle: install
	@if [ -z "$(CASE)" ]; then \
		echo "CASE variable (e.g. CASE=TS020534809) is required" >&2; \
		exit 1; \
	fi
	@level_value="$(if $(LEVEL),$(LEVEL),latest)"; \
	level_flag=""; \
	if [ -n "$(LEVEL)" ]; then level_flag="--db2level $(LEVEL)"; fi; \
	case_flag="--ibmcasenumber $(CASE)"; \
	echo "Ensuring container $(SUPPORT_CONTAINER) is running..."; \
	if ! docker ps --filter name=$(SUPPORT_CONTAINER) --format '{{.Names}}' | grep -q '^$(SUPPORT_CONTAINER)$$'; then \
		if docker ps -a --filter name=$(SUPPORT_CONTAINER) --format '{{.Names}}' | grep -q '^$(SUPPORT_CONTAINER)$$'; then \
			echo "Starting previously created container $(SUPPORT_CONTAINER)..."; \
			docker start $(SUPPORT_CONTAINER) >/dev/null; \
			sleep 5; \
		else \
			echo "Launching container via scripts.repro_runner (minimal init run)..."; \
			$(PYTHON_BIN) -m scripts.repro_runner --threads 1 --pool-size 1 --max-iterations 1 --duration 60 --log-level INFO --container-name $(SUPPORT_CONTAINER) --port $(SUPPORT_PORT) --password $(SUPPORT_PASSWORD) $$level_flag $$case_flag --keep-container || true; \
		fi; \
	fi; \
	if ! docker ps --filter name=$(SUPPORT_CONTAINER) --format '{{.Names}}' | grep -q '^$(SUPPORT_CONTAINER)$$'; then \
		echo "$(SUPPORT_CONTAINER) container is not running; unable to collect support data." >&2; \
		exit 1; \
	fi; \
	timestamp=$$(date -u +"%Y%m%dT%H%M%SZ"); \
	level_slug=$$(echo "$$level_value" | tr '/:' '__'); \
	container_tmp="/tmp/db2support_$(CASE)_$${level_slug}_$${timestamp}"; \
	archive_name="db2support_$(CASE)_$${level_slug}_$${timestamp}.zip"; \
	output_dir="docs/ibm_case-$(CASE)-$${level_slug}-$${timestamp}"; \
	mkdir -p "$$output_dir"; \
	trace_dir="$$output_dir/db2trc"; \
	mkdir -p "$$trace_dir"; \
	echo "Capturing db2trc trace (threads=$(TRACE_THREADS), max_seconds=$(TRACE_MAX_SECONDS))..."; \
	trace_rc=0; \
	if ! $(PYTHON_BIN) -m scripts.trace_capture --container-name $(SUPPORT_CONTAINER) --instance $(SUPPORT_INSTANCE) --port $(SUPPORT_PORT) --password $(SUPPORT_PASSWORD) --database $(SUPPORT_DATABASE) --threads $(TRACE_THREADS) --max-seconds $(TRACE_MAX_SECONDS) --fodc-poll-seconds $(TRACE_FODC_POLL) --fodc-quiesce-seconds $(TRACE_FODC_QUIESCE) --fodc-wait-timeout $(TRACE_FODC_TIMEOUT) --output-dir "$$trace_dir"; then \
		trace_rc=$$?; \
		echo "Trace capture exited with status $$trace_rc; continuing with available artifacts." >&2; \
	fi; \
	echo "Collecting db2support data (this may take several minutes)..."; \
	docker exec $(SUPPORT_CONTAINER) su - $(SUPPORT_INSTANCE) -c "mkdir -p $$container_tmp" >/dev/null; \
	docker exec $(SUPPORT_CONTAINER) su - $(SUPPORT_INSTANCE) -c "db2support $$container_tmp -d $(SUPPORT_DATABASE) -F -fodc AppErr -o $$archive_name"; \
	docker cp $(SUPPORT_CONTAINER):$$container_tmp/$$archive_name "$$output_dir/"; \
	docker exec $(SUPPORT_CONTAINER) su - $(SUPPORT_INSTANCE) -c "rm -rf $$container_tmp" >/dev/null; \
	fodc_parent="/database/config/$(SUPPORT_INSTANCE)/sqllib/db2dump"; \
	fodc_subdir=$$(docker exec $(SUPPORT_CONTAINER) su - $(SUPPORT_INSTANCE) -c "cd $$fodc_parent && ls -1dt FODC_* 2>/dev/null | head -n 1"); \
	if [ -n "$$fodc_subdir" ]; then \
		echo "Copying latest FODC directory $$fodc_subdir"; \
		docker cp $(SUPPORT_CONTAINER):$$fodc_parent/$$fodc_subdir "$$output_dir/$$fodc_subdir"; \
	else \
		echo "No FODC directory found under $$fodc_parent"; \
	fi; \
	docker logs $(SUPPORT_CONTAINER) > "$$output_dir/docker-logs.txt"; \
	echo "Support bundle stored in $$output_dir/$$archive_name"; \
	echo "Additional container logs saved to $$output_dir/docker-logs.txt"

container-stop:
ifeq ($(OS),Windows_NT)
	-docker rm -f $(SUPPORT_CONTAINER) >nul 2>&1 || exit 0
else
	-docker rm -f $(SUPPORT_CONTAINER) >/dev/null 2>&1
endif

clean: container-stop
ifeq ($(OS),Windows_NT)
	if exist "$(VENV)" rmdir /s /q "$(VENV)"
else
	rm -rf $(VENV)
endif
