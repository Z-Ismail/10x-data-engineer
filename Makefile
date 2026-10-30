PYTHON ?= python3

.PHONY: install test lint demo-data profile overlap engineer engineer-alone engineer-scouts engineer-graph compare clean

install:
	$(PYTHON) -m pip install -e ".[pipeline,graph]"

test:
	$(PYTHON) -m unittest discover -s tests -v

lint:
	ruff check .
	ruff format --check .

demo-data:
	$(PYTHON) demo/generate.py --seed 42 --companies 200

profile:
	$(PYTHON) -m tenx.profile demo/data --out profiles

overlap:
	$(PYTHON) -m tenx.overlap demo/data --out profiles

# The cross-source agent, then the engineer. Needs Claude Code, signed in.
# Each run starts from an empty runs/demo.
engineer: demo-data
	rm -rf runs/demo
	$(PYTHON) -m tenx.engineer --raw demo/data --goal demo/goal.md \
		--checks tests/fixtures/demo_gate.json --out runs/demo

# The same run as make engineer, as a graph saved after every step. Needs
# Claude Code, signed in. If it stops part way, carry it on with:
# python -m tenx.run resume --out runs/demo-graph
engineer-graph: demo-data
	rm -rf runs/demo-graph
	$(PYTHON) -m tenx.run run --raw demo/data --goal demo/goal.md \
		--checks tests/fixtures/demo_gate.json --out runs/demo-graph

# The engineer alone, without the cross-source agent. Needs Claude Code, signed in.
engineer-alone: demo-data
	rm -rf runs/demo-alone
	$(PYTHON) -m tenx.engineer --raw demo/data --goal demo/goal.md \
		--checks tests/fixtures/demo_gate.json --out runs/demo-alone --no-cross-source

# The default run with a scout for each raw table first. Needs Claude Code, signed in.
engineer-scouts: demo-data
	rm -rf runs/demo-scouts
	$(PYTHON) -m tenx.engineer --raw demo/data --goal demo/goal.md \
		--checks tests/fixtures/demo_gate.json --out runs/demo-scouts --scouts

# The engineer alone, with scouts, with the cross-source agent and with both,
# each run COMPARE_TRIALS times on every demo dataset (128 runs by default).
# Needs Claude Code, signed in. Run it again to resume after a stop; remove
# runs/compare to start over. Estimated above US$5, it stops before any run
# unless approved: make compare COMPARE_FLAGS=--approve-cost
COMPARE_SEEDS ?= 1 7 42 2026
COMPARE_SIZES ?= 200 1000
COMPARE_TRIALS ?= 4
COMPARE_FLAGS ?=
COMPARE_DATA = runs/compare-data
COMPARE_DATASETS = $(foreach seed,$(COMPARE_SEEDS),$(foreach size,$(COMPARE_SIZES),s$(seed)-$(size)))

compare:
	@for seed in $(COMPARE_SEEDS); do for size in $(COMPARE_SIZES); do \
		folder=$(COMPARE_DATA)/s$$seed-$$size; \
		test -d $$folder/data || $(PYTHON) demo/generate.py --seed $$seed --companies $$size \
			--out $$folder/data --answers-out $$folder/answers || exit 1; \
	done; done
	$(PYTHON) -m tenx.compare run --goal demo/goal.md --checks tests/fixtures/demo_gate.json \
		--out runs/compare --trials $(COMPARE_TRIALS) $(COMPARE_FLAGS) \
		$(foreach name,$(COMPARE_DATASETS),--raw $(name)=$(COMPARE_DATA)/$(name)/data)

clean:
	rm -rf demo/data demo/answers profiles runs
