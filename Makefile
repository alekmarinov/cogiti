# cogiti
.PHONY: test lint dist dist-assets version

VERSION  := $(shell git describe --tags --dirty 2>/dev/null || echo 0.0.0)
DIST     := cogiti-$(VERSION)
TARFLAGS := --owner=0 --group=0 --numeric-owner --sort=name --mtime=@0

version:
	@echo $(VERSION)

# What ships. Not the tests: an appliance does not run them, and shipping
# them would put the fakes on the device, which are programs that misbehave
# on purpose.
DIST_FILES := src bin docs config

dist:
	@rm -rf build/dist && mkdir -p build/dist/$(DIST)
	@tar cf - $(DIST_FILES) 2>/dev/null | tar xf - -C build/dist/$(DIST)
	@echo "$(VERSION)" > build/dist/$(DIST)/.version
	@tar cJf $(DIST).tar.xz -C build/dist $(TARFLAGS) $(DIST)
	@rm -rf build/dist
	@echo "$(DIST).tar.xz  $$(du -h $(DIST).tar.xz | cut -f1)"

# Nothing. cogiti is stdlib-only and carries no models or weights — the one
# thing it will not do is have an opinion about which model runs.
dist-assets:
	@true


test:
	@python3 -m unittest discover -s tests -p 'test_*.py' -v

lint:
	@python3 -m compileall -q src tests && echo "compiles"
