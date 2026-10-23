# ここは入れるときに、その PC で作ったものに置き換える:
#   sudo nixos-generate-config --show-hardware-config > hardware-configuration.nix
# (下は確認用の仮の中身。このままでは起動できない)
{
  fileSystems."/" = {
    device = "/dev/disk/by-label/geaspec";
    fsType = "ext4";
  };
  fileSystems."/boot" = {
    device = "/dev/disk/by-label/GEASPEC-EFI";
    fsType = "vfat";
  };
}
