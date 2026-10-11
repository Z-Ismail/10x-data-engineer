PYTHON ?= python3

.PHONY: install test demo-data profile clean

install:
	$(PYTHON) -m pip install -e ".[pipeline]"

test:
	$(PYTHON) -m unittest discover -s tests -v

demo-data:
	$(PYTHON) demo/generate.py --seed 42 --companies 200

profile:
	$(PYTHON) -m tenx.profile demo/data --out profiles

clean:
	rm -rf demo/data demo/answers profiles
