# cogiti
.PHONY: test lint

test:
	@python3 -m unittest discover -s tests -p 'test_*.py' -v

lint:
	@python3 -m compileall -q src tests && echo "compiles"
