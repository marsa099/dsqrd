{
  description = "dsqrd — native QML/Quickshell Discord client (Python daemon + vendored UI)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };

      daemon = pkgs.python3Packages.buildPythonApplication {
        pname = "dsqrd";
        version = "0.1.0";
        src = ./.;
        format = "other";
        dontBuild = true;
        nativeBuildInputs = [ pkgs.makeWrapper ];
        propagatedBuildInputs = with pkgs.python3Packages; [
          pysocks
          websocket-client
          filetype
          protobuf
          jeepney
        ];
        installPhase = ''
          runHook preInstall
          mkdir -p $out/share/dsqrd $out/bin
          cp -r dsqrd.py dchat ui codemap.json $out/share/dsqrd/
          install -Dm755 media-viewer.sh $out/share/dsqrd/media-viewer.sh
          makeWrapper ${pkgs.python3}/bin/python3 $out/bin/dsqrd \
            --add-flags "$out/share/dsqrd/dsqrd.py" \
            --prefix PYTHONPATH : "$PYTHONPATH" \
            --set DSQRD_REV "${self.rev or ""}" \
            --chdir "$out/share/dsqrd"
          runHook postInstall
        '';
        meta.mainProgram = "dsqrd";
      };

      client = pkgs.writeShellApplication {
        name = "dsqrd-client";
        runtimeInputs = [ daemon pkgs.quickshell pkgs.procps pkgs.coreutils pkgs.mpv pkgs.imv pkgs.jq pkgs.curl pkgs.xdg-utils ];
        text = ''
          export QML2_IMPORT_PATH="$HOME/.local/share/qml:${daemon}/share/dsqrd/ui/vendor''${QML2_IMPORT_PATH:+:$QML2_IMPORT_PATH}"
          export SLK_SOCK=dsqrd
          export SLK_MEDIA_VIEWER="${daemon}/share/dsqrd/media-viewer.sh"
          sock="$XDG_RUNTIME_DIR/dsqrd.sock"
          alive=""
          for pid in $(pgrep -f 'dsqrd\.py' 2>/dev/null); do
            # a zombie (unreaped child) matches pgrep but serves nothing
            case "$(ps -o stat= -p "$pid" 2>/dev/null)" in Z*|"") ;; *) alive=1 ;; esac
          done
          if [ -z "$alive" ]; then
            # The daemon binds its socket only after loading all data; drop any
            # stale socket so the wait below lands on the fresh daemon, not a
            # leftover file. Guarantees the UI's first connect gets a bootstrap.
            rm -f "$sock"
            setsid nohup ${daemon}/bin/dsqrd >/tmp/dsqrd.log 2>&1 </dev/null &
          fi
          for _ in $(seq 1 100); do [ -S "$sock" ] && break; sleep 0.1; done
          exec qs -p "${daemon}/share/dsqrd/ui"
        '';
      };
    in {
      packages.${system} = {
        dsqrd = daemon;
        dsqrd-client = client;
        default = client;
      };
      apps.${system}.default = {
        type = "app";
        program = "${client}/bin/dsqrd-client";
      };
    };
}
