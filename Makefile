PYTHON ?= python3

.PHONY: install test test-parallel lint demo-data profile overlap engineer engineer-alone engineer-scouts engineer-graph engineer-brief engineer-architect engineer-reviewer equivalence compare parity repairs compare-architect review-rates clean

install:
	$(PYTHON) -m pip install -e ".[pipeline,graph]"

test:
	$(PYTHON) -m unittest discover -s tests -v

# The same tests, one module per process, as many at a time as there are
# processors; a module that hangs is stopped after 15 minutes.
test-parallel:
	$(PYTHON) tests/parallel.py

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

# The graph run on the brief goal, which names the models, keys and money
# column but leaves the grain, the rows to leave out and the matching unsaid.
# Needs Claude Code, signed in.
engineer-brief: demo-data
	rm -rf runs/demo-brief
	$(PYTHON) -m tenx.run run --raw demo/data --goal demo/brief.md \
		--checks tests/fixtures/demo_gate.json --out runs/demo-brief

# The graph run on the brief goal with the architect agent, which writes a
# spec (SPEC.md) for the engineer first. Needs Claude Code, signed in.
engineer-architect: demo-data
	rm -rf runs/demo-architect
	$(PYTHON) -m tenx.run run --raw demo/data --goal demo/brief.md \
		--checks tests/fixtures/demo_gate.json --out runs/demo-architect --architect

# The graph run with the reviewer agent, which reads a project the gate
# passes and writes its advice to REVIEW.md. Needs Claude Code, signed in.
engineer-reviewer: demo-data
	rm -rf runs/demo-reviewer
	$(PYTHON) -m tenx.run run --raw demo/data --goal demo/goal.md \
		--checks tests/fixtures/demo_gate.json --out runs/demo-reviewer --reviewer

# The old runner and the graph on the eight comparison datasets, with a
# stand-in for Claude Code that answers the same way every time: every
# project, note, report and usage figure must be identical. No model calls.
equivalence:
	$(PYTHON) tests/equivalence.py

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

# The current runner and the graph, each with the cross-source agent, on the
# same eight datasets, PARITY_TRIALS pairs each (64 pairs, 128 runs, by
# default); the graph is non-inferior when the pass-rate difference stays
# below 10 points and its cost per pass within 10%. Needs Claude Code,
# signed in, and bubblewrap. Run it again to resume. Estimated above US$5,
# so it needs approval: make parity PARITY_FLAGS=--approve-cost
PARITY_TRIALS ?= 8
PARITY_FLAGS ?=

parity:
	@for seed in $(COMPARE_SEEDS); do for size in $(COMPARE_SIZES); do \
		folder=$(COMPARE_DATA)/s$$seed-$$size; \
		test -d $$folder/data || $(PYTHON) demo/generate.py --seed $$seed --companies $$size \
			--out $$folder/data --answers-out $$folder/answers || exit 1; \
	done; done
	$(PYTHON) -m tenx.compare paired --goal demo/goal.md --checks tests/fixtures/demo_gate.json \
		--out runs/parity --trials $(PARITY_TRIALS) $(PARITY_FLAGS) \
		$(foreach name,$(COMPARE_DATASETS),--raw $(name)=$(COMPARE_DATA)/$(name)/data)

# Repairs measured on the engineer alone: the graph with up to REPAIRS_MAX
# repairs on the same eight datasets, REPAIRS_TRIALS runs each (32 by
# default), each run compared with its own first round, a run without
# repairs. Needs Claude Code, signed in, and bubblewrap. Run it again to
# resume. Estimated above US$5, so it needs approval:
# make repairs REPAIRS_FLAGS=--approve-cost
REPAIRS_TRIALS ?= 4
REPAIRS_MAX ?= 2
REPAIRS_FLAGS ?=

repairs:
	@for seed in $(COMPARE_SEEDS); do for size in $(COMPARE_SIZES); do \
		folder=$(COMPARE_DATA)/s$$seed-$$size; \
		test -d $$folder/data || $(PYTHON) demo/generate.py --seed $$seed --companies $$size \
			--out $$folder/data --answers-out $$folder/answers || exit 1; \
	done; done
	$(PYTHON) -m tenx.compare repairs --goal demo/goal.md --checks tests/fixtures/demo_gate.json \
		--out runs/repairs --trials $(REPAIRS_TRIALS) --repairs $(REPAIRS_MAX) --no-cross-source \
		$(REPAIRS_FLAGS) $(foreach name,$(COMPARE_DATASETS),--raw $(name)=$(COMPARE_DATA)/$(name)/data)

# The graph run on the brief goal with and without the Architect, both with
# the cross-source agent, on the same eight datasets, ARCHITECT_TRIALS runs
# of each (64 runs by default), each Architect's spec scored against the
# gate's declaration. Needs Claude Code, signed in, and bubblewrap. Run it
# again to resume. Estimated above US$5, so it needs approval:
# make compare-architect ARCHITECT_FLAGS=--approve-cost
ARCHITECT_TRIALS ?= 4
ARCHITECT_FLAGS ?=

compare-architect:
	@for seed in $(COMPARE_SEEDS); do for size in $(COMPARE_SIZES); do \
		folder=$(COMPARE_DATA)/s$$seed-$$size; \
		test -d $$folder/data || $(PYTHON) demo/generate.py --seed $$seed --companies $$size \
			--out $$folder/data --answers-out $$folder/answers || exit 1; \
	done; done
	$(PYTHON) -m tenx.compare architect --goal demo/brief.md \
		--checks tests/fixtures/demo_gate.json --out runs/compare-architect \
		--trials $(ARCHITECT_TRIALS) $(ARCHITECT_FLAGS) \
		$(foreach name,$(COMPARE_DATASETS),--raw $(name)=$(COMPARE_DATA)/$(name)/data)

# The reviewer on the test bed: twelve projects that all pass the gate, six
# with one planted mistake the gate cannot see and six correct but written
# differently, each reviewed once on each of REVIEW_SEEDS at 200 companies
# (48 reviews by default), scored against the bed's answers, which the
# reviewer never sees. Needs Claude Code, signed in, and bubblewrap. Run it
# again to resume. Estimated above US$5, it stops before any review unless
# approved: make review-rates REVIEW_FLAGS=--approve-cost
REVIEW_SEEDS ?= 1 7 42 2026
REVIEW_FLAGS ?=
REVIEW_BED = runs/review-bed

review-rates:
	@for seed in $(REVIEW_SEEDS); do \
		folder=$(COMPARE_DATA)/s$$seed-200; \
		test -d $$folder/data || $(PYTHON) demo/generate.py --seed $$seed --companies 200 \
			--out $$folder/data --answers-out $$folder/answers || exit 1; \
	done
	test -f $(REVIEW_BED)/answers.json || $(PYTHON) tests/review_bed.py --out $(REVIEW_BED)
	$(PYTHON) -m tenx.review_rates run --bed $(REVIEW_BED) --goal demo/goal.md \
		--checks tests/fixtures/demo_gate.json --out runs/review-rates $(REVIEW_FLAGS) \
		$(foreach seed,$(REVIEW_SEEDS),--raw s$(seed)-200=$(COMPARE_DATA)/s$(seed)-200/data)

clean:
	rm -rf demo/data demo/answers profiles runs
