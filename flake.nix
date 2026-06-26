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
            --chdir "$out/share/dsqrd"
          runHook postInstall
        '';
        meta.mainProgram = "dsqrd";
      };

      client = pkgs.writeShellApplication {
        name = "dsqrd-client";
        runtimeInputs = [ daemon pkgs.quickshell pkgs.procps pkgs.coreutils pkgs.mpv pkgs.imv pkgs.jq pkgs.curl pkgs.xdg-utils ];
        text = ''
          export SLK_SOCK=dsqrd
          export SLK_MEDIA_VIEWER="${daemon}/share/dsqrd/media-viewer.sh"
          pgrep -f 'dsqrd\.py' >/dev/null 2>&1 || \
            setsid nohup ${daemon}/bin/dsqrd >/tmp/dsqrd.log 2>&1 </dev/null &
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
