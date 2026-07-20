# Non-Nix install (the Nix flake is the primary path). No compile step — Python —
# so `make install` bakes the current commit into a launcher that exports DSQRD_REV,
# which is what the in-app update check reads. Without it the check stays disabled.
PREFIX ?= $(HOME)/.local
REV := $(shell git rev-parse HEAD 2>/dev/null)

install:
	mkdir -p $(PREFIX)/share/dsqrd $(PREFIX)/bin
	cp -r dsqrd.py dchat ui codemap.json $(PREFIX)/share/dsqrd/
	install -Dm755 media-viewer.sh $(PREFIX)/share/dsqrd/media-viewer.sh
	printf '#!/bin/sh\nexport DSQRD_REV=%s\nexec python3 %s/share/dsqrd/dsqrd.py "$$@"\n' "$(REV)" "$(PREFIX)" > $(PREFIX)/bin/dsqrd
	chmod +x $(PREFIX)/bin/dsqrd
	@echo "installed → $(PREFIX). run: dsqrd   ·   open the UI: SLK_SOCK=dsqrd qs -p $(PREFIX)/share/dsqrd/ui"
	@echo "needs Python deps on PATH: websocket-client, pysocks, filetype, protobuf, jeepney"
	@echo "needs binaries on PATH: ffmpeg + ffprobe (GIFs, voice notes), imagemagick, mpv, imv"
	@echo "set SLK_UPDATE_CMD to your apply step for the in-app 'U' keybind."

.PHONY: install
