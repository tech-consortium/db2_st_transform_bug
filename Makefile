SHELL := /bin/sh

VENV ?= .venv
PYTHON ?= python3

ifeq ($(OS),Windows_NT)
	VENV_BIN := $(VENV)/Scripts
	PYTHON_BIN := $(VENV_BIN)/python.exe
else
	VENV_BIN := $(VENV)/bin
	PYTHON_BIN := $(VENV_BIN)/python3
endif

.PHONY: install test clean container-stop

install: $(PYTHON_BIN)

$(PYTHON_BIN): requirements.txt
	$(PYTHON) -m venv $(VENV)
	$(PYTHON_BIN) -m pip install --upgrade pip
	$(PYTHON_BIN) -m pip install -r requirements.txt

test: install
	$(PYTHON_BIN) -m scripts.repro_runner $(ARGS)

container-stop:
	-docker rm -f db2-st-transform >/dev/null 2>&1

clean: container-stop
	rm -rf $(VENV)
