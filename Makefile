PYTHON ?= 3.14
VENV := .venv
BIN := $(VENV)/bin
SRC := custom_components/mentat

.PHONY: venv lint format typecheck test check hassfest deploy

venv:
	uv venv --python $(PYTHON) $(VENV)
	uv pip install --python $(BIN)/python -r requirements-test.txt

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck:
	$(BIN)/mypy --strict --ignore-missing-imports $(SRC)

test:
	$(BIN)/pytest

check: lint typecheck test

# The image is published for amd64 only, so Apple silicon runs it under emulation.
hassfest:
	docker run --rm --platform linux/amd64 -v "$$PWD":/github/workspace ghcr.io/home-assistant/hassfest

# Copy the integration to a Home Assistant host over SSH, which needs an SSH
# app there. HA_HOST may carry a user: make deploy HA_HOST=root@192.0.2.10
# rsync is used when both ends have it, and removes files the repository no longer
# has; otherwise scp copies over what is there.
HA_HOST ?= homeassistant.local
HA_CONFIG ?= /config
HA_DEST = $(HA_CONFIG)/custom_components/mentat

deploy:
ifeq ($(origin HA_HOST),file)
	@echo "HA_HOST is not set, so this copies to $(HA_HOST)."
	@echo "Set it to choose the host: make deploy HA_HOST=root@192.0.2.10"
endif
	@test -n "$(HA_HOST)" || { echo "HA_HOST is empty. Give the host: make deploy HA_HOST=root@192.0.2.10"; exit 1; }
	@if command -v rsync >/dev/null 2>&1 && ssh $(HA_HOST) command -v rsync >/dev/null 2>&1; then \
		rsync -rltv --delete --exclude __pycache__ $(SRC)/ $(HA_HOST):$(HA_DEST)/; \
	else \
		ssh $(HA_HOST) mkdir -p $(HA_CONFIG)/custom_components && \
		scp -r $(SRC) $(HA_HOST):$(HA_CONFIG)/custom_components/; \
	fi
	@echo "Copied to $(HA_HOST):$(HA_DEST). Restart Home Assistant to load it."
