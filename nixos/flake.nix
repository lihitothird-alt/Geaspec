{
  # Geaspec OS (試作): 貸し手の PC を、借りた人が使うたびにまっさらに戻る NixOS にする。
  # 入れ方は README.md。
  description = "Geaspec OS: a lender PC that is wiped after every rental";

  # GitHub ではなく NixOS の配布元から取る (どの PC でも同じ中身になるよう、flake.lock で固定する)
  inputs.nixpkgs.url = "https://channels.nixos.org/nixos-26.05/nixexprs.tar.xz";

  outputs = { nixpkgs, ... }: {
    nixosConfigurations.geaspec = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        ./geaspec.nix
        # この PC の部品の設定 (入れるときに nixos-generate-config で作ってコピーする)
        ./hardware-configuration.nix
        ./local.nix
      ];
    };
  };
}
