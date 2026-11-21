IMAGE ?= fog-of-war
PY ?= .venv/bin/python
# The organiser sample (patches and tests.json); override SAMPLE to point elsewhere.
SAMPLE ?= $(abspath ../kits/rwx/x/sample)
OUTPUT := $(CURDIR)/output.jsonl
SCORE := $(CURDIR)/vendor/score.py

# The flags the scorer runs the image with (same as the kit Makefile).
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

.PHONY: build sample check test eval presubmit

build:
	docker build --platform linux/amd64 --tag $(IMAGE) .

sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 $(SCORE) $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

check: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 $(SCORE) $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) > /dev/null && echo "format ok"

test:
	$(PY) -m pytest tests_sel -q -o addopts=""

# make eval SET=sample   (native)   |   make eval SET=sample DOCKER=1
SET ?= sample
eval:
	$(PY) tools/eval.py --set $(SET) $(if $(DOCKER),--docker,)

presubmit:
	bash tools/presubmit.sh

# Laptop only. SRC = sqlglot checkout at the pinned SHA, RUN = instrumented run dirs (see tools/build_maps.py).
.PHONY: maps
maps:
	$(PY) tools/build_maps.py build --src $(SRC) --run $(RUN) --tests $(SAMPLE)/tests.json --flaky $(FLAKY) --out maps --python $(PY)

# Laptop only. Value-read map (maps/valreads.bin): run tools/run_valmap.sh once per hash seed (SEED=0, 1, 2) into
# VMRUN dirs, then build. SRC = sqlglot checkout at the pinned SHA.
.PHONY: valmap
valmap:
	$(PY) tools/build_valmap.py build --maps maps --run $(VMRUN) --out maps/valreads.bin
