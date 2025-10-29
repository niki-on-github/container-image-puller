{
  description = "FastAPI Pull Service Python Dev Environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        pythonPackages = ps: with ps; [
          fastapi
          uvicorn
          ipaddress
        ];
        pythonEnv = pkgs.python311.withPackages pythonPackages;
      in {
        devShells.default = pkgs.mkShell {
          buildInputs = [ pythonEnv ];
          shellHook = ''
            echo "🐍 FastAPI Pull Service dev shell active"
            echo "Python version: $(python --version)"
          '';
        };
      }
    );
}
