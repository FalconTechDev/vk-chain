; Inno Setup script for vk-chain. Built by build.py, which passes the version:
;   ISCC /DAppVersion=2.0.0 installer\vk-chain.iss
; Saved as UTF-8 with a byte-order mark - without one Inno reads the Russian
; text below as ANSI and shows garbage.

#ifndef AppVersion
  #define AppVersion "2.0.0"
#endif

[Setup]
AppId={{CEA9D58C-DEF6-493B-A70C-8984701E6A9F}
AppName=vk-chain
AppVersion={#AppVersion}
AppVerName=vk-chain {#AppVersion}
AppPublisher=FalconTechDev
AppPublisherURL=https://github.com/FalconTechDev/vk-chain
AppSupportURL=https://github.com/FalconTechDev/vk-chain/issues
AppUpdatesURL=https://github.com/FalconTechDev/vk-chain/releases
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\vk-chain
DefaultGroupName=vk-chain
DisableProgramGroupPage=yes
; Installs for the current user by default, so no administrator rights and no
; UAC prompt. The dialog still lets someone choose an all-users install.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=vk-chain-setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\vk-chain.exe
UninstallDisplayName=vk-chain
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; An upgrade over a running copy would otherwise fail on its locked files.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\vk-chain\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\vk-chain"; Filename: "{app}\vk-chain.exe"
Name: "{autodesktop}\vk-chain"; Filename: "{app}\vk-chain.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\vk-chain.exe"; Description: "{cm:LaunchProgram,vk-chain}"; Flags: nowait postinstall skipifsilent

[Code]
const
  // Relative to SOFTWARE, so the three places it can be registered are each
  // spelled out once below. (An earlier version already began this with
  // 'Software' and then prefixed it again, which named a key that does not
  // exist and reported WebView2 missing on a machine that has it.)
  WebView2Key = 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

// The window is drawn by Microsoft's WebView2, which Windows 11 ships with and
// Windows 10 often does not. Say so before installing, rather than leaving a
// program that starts and shows an empty window.
function WebView2Installed(): Boolean;
var
  Version: String;
begin
  Result :=
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\' + WebView2Key, 'pv', Version) or
    RegQueryStringValue(HKLM, 'SOFTWARE\' + WebView2Key, 'pv', Version) or
    RegQueryStringValue(HKCU, 'Software' + chr(92) + WebView2Key, 'pv', Version);
  if Result then
    Result := (Version <> '') and (Version <> '0.0.0.0');
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not WebView2Installed() then
    Result := MsgBox(
      'Для окна vk-chain нужен Microsoft Edge WebView2 Runtime, и он не найден.' + #13#10 + #13#10 +
      'В Windows 11 он есть всегда. Для Windows 10 скачай его бесплатно: ' + #13#10 +
      'https://go.microsoft.com/fwlink/p/?LinkId=2124703' + #13#10 + #13#10 +
      'Продолжить установку?', mbConfirmation, MB_YESNO or MB_DEFBUTTON1) = IDYES;
end;

// Uninstalling removes the program, not what it learned. The cache is hours
// of requests and the token is a credential, so each is asked about on its
// own, and both default to No.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir, TokenFile: String;
begin
  if CurUninstallStep <> usPostUninstall then Exit;
  DataDir := ExpandConstant('{userappdata}\vk-chain');
  TokenFile := ExpandConstant('{%USERPROFILE}\.vk_chain_token');
  if DirExists(DataDir) then
    if MsgBox('Удалить и сохранённые данные: кэш друзей, историю, настройки?',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(DataDir, True, True, True);
  if FileExists(TokenFile) then
    if MsgBox('Удалить сохранённый токен VK?', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DeleteFile(TokenFile);
end;
