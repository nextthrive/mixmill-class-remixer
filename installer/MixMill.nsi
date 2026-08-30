Unicode true
RequestExecutionLevel user
SetCompressor /SOLID lzma
ShowInstDetails show
ShowUninstDetails show

!ifndef APP_VERSION
  !define APP_VERSION "1.0.0"
!endif
!ifndef APP_VERSION_NUMERIC
  !define APP_VERSION_NUMERIC "1.0.0.0"
!endif

!define APP_NAME "MixMill"
!define APP_DISPLAY_NAME "MixMill Desktop"
!define APP_EXE "MixMill.exe"
!define APP_MUTEX "Local\MixMillDesktop-4B35C76A"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\MixMill"
!define INNO_UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\{A9614875-B4E9-4E38-A06B-17D17FC8A5AE}_is1"
!define WEBVIEW_CLIENT_ID "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

Name "${APP_DISPLAY_NAME} ${APP_VERSION}"
OutFile "..\artifacts\MixMill-${APP_VERSION}-Windows-x64-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"
InstallDirRegKey HKCU "${UNINSTALL_KEY}" "InstallLocation"
Icon "..\.build\MixMill.ico"
UninstallIcon "..\.build\MixMill.ico"
BrandingText "MixMill"

VIProductVersion "${APP_VERSION_NUMERIC}"
VIAddVersionKey /LANG=1033 "ProductName" "${APP_DISPLAY_NAME}"
VIAddVersionKey /LANG=1033 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "FileDescription" "MixMill Desktop Installer"
VIAddVersionKey /LANG=1033 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "CompanyName" "MixMill"
VIAddVersionKey /LANG=1033 "LegalCopyright" "GNU AGPL version 3 or later"

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "WinVer.nsh"

!define MUI_ABORTWARNING
!define MUI_LICENSEPAGE_CHECKBOX
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "Launch MixMill"
!define MUI_FINISHPAGE_LINK "Read privacy and support information"
!define MUI_FINISHPAGE_LINK_LOCATION "$INSTDIR\PRIVACY.md"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "..\LICENSE"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "English"

Var WebViewFound

Function CheckAppNotRunning
  System::Call 'kernel32::OpenMutexW(i 0x00100000, i 0, w "${APP_MUTEX}") i .r0'
  ${If} $0 <> 0
    System::Call 'kernel32::CloseHandle(i r0)'
    IfSilent 0 +2
      SetErrorLevel 2
    MessageBox MB_ICONEXCLAMATION|MB_OK "MixMill is running. Close it, then run setup again."
    Abort
  ${EndIf}
FunctionEnd

Function CheckWindowsVersion
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP|MB_OK "MixMill requires 64-bit Windows 10 version 1809 or newer, or Windows 11."
    Abort
  ${EndIf}
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP|MB_OK "MixMill requires 64-bit Windows."
    Abort
  ${EndIf}
  ReadRegStr $0 HKLM "SOFTWARE\Microsoft\Windows NT\CurrentVersion" "CurrentBuildNumber"
  IntCmp $0 17763 windows_ok windows_too_old windows_ok
  windows_too_old:
    MessageBox MB_ICONSTOP|MB_OK "MixMill requires Windows 10 version 1809 (build 17763) or newer."
    Abort
  windows_ok:
FunctionEnd

Function DetectWebView2
  StrCpy $WebViewFound "0"
  SetRegView 32
  ReadRegStr $0 HKCU "Software\Microsoft\EdgeUpdate\Clients\${WEBVIEW_CLIENT_ID}" "pv"
  ${If} $0 != ""
  ${AndIf} $0 != "0.0.0.0"
    StrCpy $WebViewFound "1"
    Return
  ${EndIf}
  SetRegView 64
  ReadRegStr $0 HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\${WEBVIEW_CLIENT_ID}" "pv"
  ${If} $0 != ""
  ${AndIf} $0 != "0.0.0.0"
    StrCpy $WebViewFound "1"
    SetRegView 32
    Return
  ${EndIf}
  SetRegView 32
  ReadRegStr $0 HKLM "SOFTWARE\Microsoft\EdgeUpdate\Clients\${WEBVIEW_CLIENT_ID}" "pv"
  ${If} $0 != ""
  ${AndIf} $0 != "0.0.0.0"
    StrCpy $WebViewFound "1"
  ${EndIf}
FunctionEnd

Function .onInit
  SetShellVarContext current
  Call CheckWindowsVersion
  Call CheckAppNotRunning
FunctionEnd

Section "MixMill (required)" SEC_MAIN
  SectionIn RO

  Call DetectWebView2
  ${If} $WebViewFound != "1"
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    File /oname=MicrosoftEdgeWebview2Setup.exe "..\.build\MicrosoftEdgeWebview2Setup.exe"
    DetailPrint "Installing Microsoft Edge WebView2 Runtime..."
    ExecWait '"$PLUGINSDIR\MicrosoftEdgeWebview2Setup.exe" /silent /install' $0
    ${If} $0 != 0
      MessageBox MB_ICONSTOP|MB_OK "Microsoft Edge WebView2 installation failed (exit code $0). MixMill setup cannot continue."
      SetErrorLevel $0
      Abort
    ${EndIf}
  ${EndIf}

  SetOutPath "$INSTDIR"
  File /r "..\dist\MixMill\*"

  ; Remove leftovers when upgrading from the old Inno Setup package.
  Delete "$INSTDIR\unins000.exe"
  Delete "$INSTDIR\unins000.dat"
  Delete "$INSTDIR\unins000.msg"
  DeleteRegKey HKCU "${INNO_UNINSTALL_KEY}"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\MixMill.lnk" "$INSTDIR\${APP_EXE}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\MixMill - Change Media Folder.lnk" "$INSTDIR\${APP_EXE}" "--choose-media"

  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${APP_DISPLAY_NAME} ${APP_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "MixMill"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
  SectionGetSize ${SEC_MAIN} $0
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "EstimatedSize" $0
SectionEnd

Section /o "Desktop shortcut" SEC_DESKTOP
  CreateShortcut "$DESKTOP\MixMill.lnk" "$INSTDIR\${APP_EXE}"
SectionEnd

Function un.CheckAppNotRunning
  System::Call 'kernel32::OpenMutexW(i 0x00100000, i 0, w "${APP_MUTEX}") i .r0'
  ${If} $0 <> 0
    System::Call 'kernel32::CloseHandle(i r0)'
    IfSilent 0 +2
      SetErrorLevel 2
    MessageBox MB_ICONEXCLAMATION|MB_OK "MixMill is running. Close it, then uninstall again."
    Abort
  ${EndIf}
FunctionEnd

Function un.onInit
  SetShellVarContext current
  Call un.CheckAppNotRunning
FunctionEnd

Section "Uninstall"
  Delete "$DESKTOP\MixMill.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\MixMill.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\MixMill - Change Media Folder.lnk"
  RMDir "$SMPROGRAMS\${APP_NAME}"

  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\PRIVACY.md"
  Delete "$INSTDIR\SUPPORT.md"
  Delete "$INSTDIR\SECURITY.md"
  Delete "$INSTDIR\THIRD_PARTY_NOTICES.md"
  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"

  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  ; User data deliberately stays in %LOCALAPPDATA%\MixMill.
SectionEnd
