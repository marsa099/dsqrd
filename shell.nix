{ pkgs ? import <nixpkgs> { } }:

# Own Python env for dsqrd — the deps dchat needs, no endcord.
pkgs.mkShell {
  packages = [
    (pkgs.python3.withPackages (ps: with ps; [
      pysocks
      websocket-client
      filetype
      protobuf
      jeepney
    ]))
  ];
}
