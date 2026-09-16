{
  description = "Mindprint render node: a NixOS module for a GPU desktop that pulls image-generation work from Mindprint over outbound HTTPS";

  # No inputs on purpose: the module takes pkgs from the machine that imports it, so this flake
  # never pins a nixpkgs of its own and never needs a lock update for one.
  outputs = { self, ... }: {
    nixosModules.default = import ./mindprint-render.nix;
    nixosModules.mindprint-render = self.nixosModules.default;
  };
}
