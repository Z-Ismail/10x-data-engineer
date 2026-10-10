PYTHON ?= python3

.PHONY: test demo-data clean

test:
	$(PYTHON) -m unittest discover -s tests -v

demo-data:
	$(PYTHON) demo/generate.py --seed 42 --companies 200

clean:
	rm -rf demo/data demo/answers
