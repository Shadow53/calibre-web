{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  inputs.poetry2nix.url = "github:nix-community/poetry2nix";

  outputs = { self, nixpkgs, poetry2nix }:
    let
      supportedSystems = [ "x86_64-linux" "x86_64-darwin" "aarch64-linux" "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
      pkgs = forAllSystems (system: nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (system: let
        inherit (poetry2nix.lib.mkPoetry2Nix { pkgs = pkgs.${system}; }) mkPoetryApplication;
      in {
        default = mkPoetryApplication { projectDir = self; };
      });


      devShells = forAllSystems (system: let
        inherit (poetry2nix.lib.mkPoetry2Nix { pkgs = pkgs.${system}; }) mkPoetryEnv defaultPoetryOverrides;

        addModules = (self: super: modules: builtins.listToAttrs (map (obj: let
          extras = [self.flit-core] ++ (map (name: self.${name}) obj.modules) ++ (map (name: pkgs.${system}.${name}) (obj.pkgs or []));
        in {
          name = obj.name;
          value = super.${obj.name}.overridePythonAttrs (old: {
            buildInputs = (old.buildInputs or []) ++ extras;
            nativeBuildInputs = (old.nativeBuildInputs or []) ++ extras;
          });
        }) modules ));

        addSetuptools = (self: super: names: (addModules
          self
          super
          (map (name: {
            inherit name;
            modules = [ "setuptools" ];
          }) names)
        ));
      in {
        default = pkgs.${system}.mkShellNoCC {
          packages = with pkgs.${system}; [
            (mkPoetryEnv {
              overrides = defaultPoetryOverrides.extend (self: super: {
                  comicapi = super.comicapi.overridePythonAttrs (old: {
                    format = "setuptools";
                    src = fetchFromGitHub {
                      owner = "comictagger";
                      repo = old.pname;
                      rev = "2bf8332114e49add0bbc0fd3d85bdbba02de3d1a";
                      hash = "sha256-Cd3ILy/4PqWUj1Uu9of9gCpdVp2R6CXjPOuSXgrB894=";
                    };
                  });
                  cryptography = super.cryptography.overridePythonAttrs (old: rec {
                    cargoDeps = pkgs.${system}.rustPlatform.fetchCargoTarball {
                      inherit (old) src;
                      name = "${old.pname}-${old.version}";
                      sourceRoot = "${old.pname}-${old.version}/${cargoRoot}";
                      sha256 = "sha256-Pw3ftpcDMfZr/w6US5fnnyPVsFSB9+BuIKazDocYjTU=";
                    };
                    cargoRoot = "src/rust";
                  });
                  levenshtein = super.levenshtein.overridePythonAttrs (old: {
                    dontUseCmakeConfigure = true;
                    nativeBuildInputs = old.nativeBuildInputs ++ [
                      self.cmake
                      self.scikit-build
                    ];
                  });
                } // (addModules self super [
                  { name = "flask_dance"; modules = ["flit-core" "flit_core"]; }
                  { name = "flask-dance"; modules = ["flit-core" "flit_core"]; }
                  { name = "flask-oidc"; modules = ["poetry"]; }
                  { name = "wtforms"; modules = ["babel" "hatchling"]; }
                ]) // (addSetuptools self super [
                  "inflate64"
                  "multivolumefile"
                  "pybcj"
                  "pyppmd"
                  "pyzstd"
                  "py7zr"
                  "urlobject"
                  "free-proxy"
                  "rauth"
                  "flask-dance"
                  "flask-simpleldap"
                  "goodreads"
                  "text2digits"
                  "wordninja"
                  "scholarly"
                ])
              );
              projectDir = self;
            })
            poetry
          ];
        };
      });
    };
}
