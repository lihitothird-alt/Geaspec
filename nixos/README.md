# Geaspec OS（試作）

貸し手の PC を、**借りた人が使うたびにまっさらに戻る** NixOS にする設定です。いまは 1 台で試すための試作です。

## 何が安全になるか

- **起動するたびに、借りた人のファイルを消す**。Steam と VRChat のログイン情報、履歴、設定も消えます。残るのは、Steam で入れたゲーム本体（VRChat など）だけです。
- 借りた人は「borrower」というユーザーで使い、**管理者の権限はありません**。
- 貸し手のふだんの Windows は**別の SSD**にあります。借りた人がそれを開くには、管理者のパスワードがいります。
- どの PC も、このフォルダの設定ファイルだけで**同じ中身**になります（`flake.lock` で版を固定）。

## Virtual Desktop との違い

Virtual Desktop は Windows でしか動きません。そのため、Quest への映像は **WiVRn**（Linux で動く無料のソフト）で送ります。

- VRChat は OpenVR 向けのゲームなので、**xrizer** を通して WiVRn につなぎます。
- Quest には WiVRn のアプリを入れます。

## 用意するもの

- 空いている SSD（内蔵か USB の外付け、**64GB 以上**、中身は消えます）
- 8GB 以上の USB メモリ（NixOS を入れるのに使う）
- NVIDIA か AMD のグラフィックボード

## 入れ方

1. https://nixos.org/download/ から **NixOS 26.05 のグラフィカル版**の ISO をダウンロードする。
2. Rufus などで USB メモリに書き込み、そこから PC を起動する。
3. インストーラーで、**空いている SSD を選んで**入れる。
   - **Windows が入っているディスクを選ばないように注意**してください。全部消えます。
   - ユーザー名は `geaspec` にします。
4. 入れた NixOS で端末を開き、次を実行する。
   ```sh
   git clone https://github.com/lihitothird-alt/Geaspec.git
   cd Geaspec/nixos
   sudo nixos-generate-config --show-hardware-config > hardware-configuration.nix
   nano local.nix        # グラフィックボード (nvidia / amd) と PC の名前を書き換える
   sudo nixos-rebuild switch --flake .#geaspec
   sudo tailscale up     # 出てきたリンクを開いて、Geaspec の Tailscale にログインする
   ```
5. 再起動する。自動で「Geaspec」のユーザーで画面が開けば完了です。

## 試すこと

1. Steam を開いて**自分のアカウント**でログインし、VRChat を入れる。
   - ゲーム本体は、次に起動したあとも残ります。
2. Quest に WiVRn のアプリを入れる。
3. WiVRn のアプリで、この PC の Tailscale の IP アドレスにつなぐ。
4. VRChat を起動して、**FPS と遅れ**を Windows + Virtual Desktop のときと比べる。
5. PC を再起動して、**Steam と VRChat のログインが消えていること**を確かめる。

Proton（Windows のゲームを動かす部分）も毎回作り直します。そのため、VRChat の最初の起動は 1 分ほど長くかかります。

## 管理するとき

- 管理者のユーザー `geaspec` でログインし直す（画面の左下 → ログアウト → ユーザーを切り替え）。
- 設定を変えたら `sudo nixos-rebuild switch --flake .#geaspec` を実行する。
