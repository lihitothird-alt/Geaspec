; Geaspec Agent のインストーラー (Inno Setup 6)
; GitHub Actions が  ISCC /DAppVersion=x.y.z packaging\installer.iss  でビルドする。
;
; ユーザーごとのインストール (管理者権限なし) にしている。
; Agent は「貸し出し専用ユーザー」の VRChat データ (HKCU・AppData) を消すため、
; 管理者として別アカウントに入れると対象を取り違えるおそれがあるから。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Geaspec Agent"
; 署名付きの公式 Python で host_agent.py を動かす (自前 exe の誤検知を避けるため)
#define AppExe "python\pythonw.exe"
#define AppScript "geaspec_gui.py"

[Setup]
AppId={{8F1B6C55-5B0E-4C4B-9E52-7A1E3B0B6A21}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Geaspec
DefaultDirName={localappdata}\Programs\Geaspec Agent
; 画面は出さずにすぐインストールし、終わったらアプリを開く。
; 「貸し出し専用ユーザーか」の確認とログインは、アプリの最初の画面で行う (同じことを 2 回聞かない)
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=GeaspecAgent-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
; 動いている Agent (コンソール) は Windows の「アプリケーションの終了」機能では閉じられず、
; インストールが「アプリケーションを終了しています」で止まるため、PrepareToInstall で自分で終了させる
CloseApplications=no

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Files]
Source: "..\dist\GeaspecAgent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; 0.2.0 までの自前 exe (PyInstaller 製) が残っていると誤検知の原因になるので消す
Type: files; Name: "{app}\GeaspecAgent.exe"
Type: filesandordirs; Name: "{app}\_internal"
; 0.2.x までのコンソール版のショートカット (0.3.0 からは画面のボタンで操作する)
Type: files; Name: "{userprograms}\Geaspec\Geaspec Agent（待機を開始）.lnk"
Type: files; Name: "{userprograms}\Geaspec\削除対象の確認（何も消しません）.lnk"
Type: files; Name: "{userprograms}\Geaspec\VRChat の痕跡を今すぐ削除.lnk"
Type: files; Name: "{userprograms}\Geaspec\接続設定の変更.lnk"
Type: files; Name: "{userprograms}\Geaspec\ログとレポートのフォルダ.lnk"

[Icons]
Name: "{userprograms}\Geaspec\Geaspec Agent"; Filename: "{app}\{#AppExe}"; Parameters: """{app}\{#AppScript}"""; WorkingDir: "{app}"
Name: "{userprograms}\Geaspec\動きの記録のフォルダ"; Filename: "{localappdata}\GeaspecAgent"
Name: "{userprograms}\Geaspec\アンインストール"; Filename: "{uninstallexe}"
; サインインしたら自動で開く。貸し出し専用に登録していないユーザーでは何も消さないので、常に入れる
Name: "{userstartup}\Geaspec Agent"; Filename: "{app}\{#AppExe}"; Parameters: """{app}\{#AppScript}"""; WorkingDir: "{app}"

[Registry]
; 説明ページの「Geaspec アプリに戻る」ボタン (geaspec://open) で、このアプリを開く (開いていれば前に出す)
Root: HKCU; Subkey: "Software\Classes\geaspec"; ValueType: string; ValueName: ""; ValueData: "URL:Geaspec"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\geaspec"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\geaspec\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""{app}\{#AppScript}"" ""%1"""

[Dirs]
Name: "{localappdata}\GeaspecAgent"

[Run]
Filename: "{app}\{#AppExe}"; Parameters: """{app}\{#AppScript}"""; WorkingDir: "{app}"; Flags: nowait skipifsilent

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  Params: String;
begin
  Result := '';
  { このユーザーの、インストール先から起動された Agent だけを終了する (他のアプリには触らない) }
  Params := '-NoProfile -ExecutionPolicy Bypass -Command "$app = ''' + ExpandConstant('{app}') + '''; ' +
    'Get-Process -Name GeaspecAgent,python,pythonw -ErrorAction SilentlyContinue | ' +
    'Where-Object { $_.Path -and $_.Path.StartsWith($app, [System.StringComparison]::OrdinalIgnoreCase) } | ' +
    'Stop-Process -Force"';
  Exec('powershell.exe', Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1000);
end;
