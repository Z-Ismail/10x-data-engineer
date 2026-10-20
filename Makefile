PYTHON ?= python3

.PHONY: install test demo-data profile overlap engineer engineer-cross-source clean

install:
	$(PYTHON) -m pip install -e ".[pipeline]"

test:
	$(PYTHON) -m unittest discover -s tests -v

demo-data:
	$(PYTHON) demo/generate.py --seed 42 --companies 200

profile:
	$(PYTHON) -m tenx.profile demo/data --out profiles

overlap:
	$(PYTHON) -m tenx.overlap demo/data --out profiles

# Needs Claude Code, signed in. Each run starts from an empty runs/demo.
engineer: demo-data
	rm -rf runs/demo
	$(PYTHON) -m tenx.engineer --raw demo/data --goal demo/goal.md \
		--checks tests/fixtures/demo_gate.json --out runs/demo

# The same run with the cross-source agent first. Needs Claude Code, signed in.
engineer-cross-source: demo-data
	rm -rf runs/demo-cross-source
	$(PYTHON) -m tenx.engineer --raw demo/data --goal demo/goal.md \
		--checks tests/fixtures/demo_gate.json --out runs/demo-cross-source --cross-source

clean:
	rm -rf demo/data demo/answers profiles runs
