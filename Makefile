PYTHON ?= python3
RUNTIME_DIR ?= $(CURDIR)/runtime
ASSET_DIR ?= $(CURDIR)/data/pal
GAME_BIN ?= $(CURDIR)/vendor/sdlpal/unix/sdlpal

.PHONY: assets build-game run-game debug clean-runtime

assets:
	$(PYTHON) scripts/fetch_assets.py --target "$(ASSET_DIR)"

build-game:
	$(MAKE) -C vendor/sdlpal/unix all

run-game:
	mkdir -p "$(RUNTIME_DIR)"
	cd "$(ASSET_DIR)" && PALPAL_AGENT=1 PALPAL_RUNTIME_DIR="$(RUNTIME_DIR)" PALPAL_SAVE_SLOT=0 "$(GAME_BIN)"

debug:
	mkdir -p "$(RUNTIME_DIR)"
	$(PYTHON) -m palpal.debug_server --runtime-dir "$(RUNTIME_DIR)" --asset-dir "$(ASSET_DIR)" --game-binary "$(GAME_BIN)"

clean-runtime:
	rm -rf "$(RUNTIME_DIR)"

