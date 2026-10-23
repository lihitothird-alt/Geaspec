# Geaspec OS (試作): 貸し手の PC を、借りた人が使うたびにまっさらに戻る NixOS にする。
#
# - 借りる人用のユーザー (borrower) で自動でログインする。管理者の権限はない
# - 起動するたびに、borrower のファイルを消す。残すのは Steam のゲーム本体 (VRChat など) だけ
#   Steam のログイン情報・VRChat のログイン情報 (Proton の中に入っている) も消す
# - Quest へは WiVRn で映像を送る (Virtual Desktop の代わり。Linux で動く無料のソフト)
#   VRChat は OpenVR のゲームなので、xrizer で WiVRn (OpenXR) につなぐ
# - 貸し手のふだんの Windows とデータは、別の SSD にあるので触れない
#   (内蔵のディスクをつなぐには管理者のパスワードがいる)
{ config, lib, pkgs, ... }:

let
  cfg = config.geaspec;
  borrower = "borrower";
  home = "/home/${borrower}";
  steam = "${home}/.local/share/Steam";
  # VRChat (OpenVR) を WiVRn (OpenXR) で動かすための設定
  vrpath = pkgs.writeText "openvrpaths.vrpath" (builtins.toJSON {
    config = [ "${steam}/config" ];
    external_drivers = null;
    jsonid = "vrpathreg";
    log = [ "${steam}/logs" ];
    runtime = [ "${pkgs.xrizer}/lib/xrizer" ];
    version = 1;
  });
in
{
  options.geaspec.gpu = lib.mkOption {
    type = lib.types.enum [ "nvidia" "amd" ];
    description = "この PC のグラフィックボードの種類";
  };

  config = {
    system.stateVersion = "26.05";
    # Steam と NVIDIA のドライバーは、中身が公開されていないソフト
    nixpkgs.config.allowUnfree = true;

    boot.loader.systemd-boot.enable = true;
    boot.loader.efi.canTouchEfiVariables = true;
    boot.tmp.useTmpfs = true;

    time.timeZone = "Asia/Tokyo";
    i18n.defaultLocale = "ja_JP.UTF-8";
    fonts.packages = [ pkgs.noto-fonts-cjk-sans ];
    networking.networkmanager.enable = true;
    # 借りる人の Quest とは Tailscale でつなぐ (家の外からでも届く)
    services.tailscale.enable = true;

    # --- 画面: KDE Plasma に、借りる人用のユーザーで自動でログインする ---
    services.displayManager.sddm.enable = true;
    services.displayManager.sddm.wayland.enable = true;
    services.desktopManager.plasma6.enable = true;
    services.displayManager.autoLogin = {
      enable = true;
      user = borrower;
    };
    # 借りている間に画面がロックされたり、PC がスリープしたりしないように
    environment.etc."xdg/kscreenlockerrc".text = ''
      [Daemon]
      Autolock=false
      LockOnResume=false
    '';
    systemd.targets.sleep.enable = false;
    systemd.targets.suspend.enable = false;
    systemd.targets.hibernate.enable = false;
    systemd.targets.hybrid-sleep.enable = false;

    # --- グラフィックボード ---
    hardware.graphics = {
      enable = true;
      enable32Bit = true;
    };
    services.xserver.videoDrivers = lib.mkIf (cfg.gpu == "nvidia") [ "nvidia" ];
    hardware.nvidia = lib.mkIf (cfg.gpu == "nvidia") {
      modesetting.enable = true;
      open = true;
    };

    # --- VR: Steam と WiVRn ---
    programs.steam.enable = true;
    services.wivrn = {
      enable = true;
      openFirewall = true;
      autoStart = true;
      highPriority = true;
      steam.importOXRRuntimes = true;
    };

    # --- ユーザー ---
    users.users.${borrower} = {
      isNormalUser = true;
      uid = 1001;
      description = "Geaspec";
      # パスワードではログインできない (自動ログインだけ)。管理者の権限もない
      hashedPassword = "!";
    };
    # 貸し手 (管理する人)。入れたあとに `passwd geaspec` でパスワードを決める
    users.users.geaspec = {
      isNormalUser = true;
      description = "Geaspec 管理";
      extraGroups = [ "wheel" "networkmanager" ];
    };

    # --- 起動するたびに、借りる人のファイルを消す (画面にログインする前) ---
    systemd.services.geaspec-wipe = {
      description = "Geaspec: wipe the borrower's files";
      wantedBy = [ "multi-user.target" ];
      before = [ "display-manager.service" ];
      serviceConfig.Type = "oneshot";
      path = [ pkgs.coreutils pkgs.findutils ];
      script = ''
        h=${home}
        s=${steam}
        # 借りた人がフォルダを「ほかの場所へのリンク」にすり替えていても、その先は消さない (リンクだけ消す)
        for p in .local .local/share .local/share/Steam .local/share/Steam/steamapps; do
          if [ -L "$h/$p" ]; then rm -f "$h/$p"; fi
        done
        mkdir -p "$s"
        # Steam のゲーム本体 (~/.local/share/Steam) 以外は全部消す
        find "$h" -mindepth 1 -maxdepth 1 ! -name .local -exec rm -rf {} +
        find "$h/.local" -mindepth 1 -maxdepth 1 ! -name share -exec rm -rf {} +
        find "$h/.local/share" -mindepth 1 -maxdepth 1 ! -name Steam -exec rm -rf {} +
        # Steam の中のログイン情報・ユーザーごとのデータ・ブラウザのクッキー
        rm -rf "$s/config" "$s/userdata" "$s/logs" "$s"/ssfn*
        # Proton (Windows のゲームを動かす部分) の中身。VRChat のログイン情報はここに入っている
        rm -rf "$s/steamapps/compatdata"
        # VRChat を WiVRn で動かす設定を置き直す
        mkdir -p "$h/.config/openvr"
        cp ${vrpath} "$h/.config/openvr/openvrpaths.vrpath"
        chmod 644 "$h/.config/openvr/openvrpaths.vrpath"
        chown -hR ${borrower}:users "$h"
        chmod 700 "$h"
      '';
    };

    environment.systemPackages = [ pkgs.xrizer ];
  };
}
