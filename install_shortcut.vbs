Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

scriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
vbsLauncher = FSO.BuildPath(scriptDir, "AGSHome.vbs")

' Desktop shortcut
desktopPath = WshShell.SpecialFolders("Desktop")
Set lnk = WshShell.CreateShortcut(FSO.BuildPath(desktopPath, "AGSHome Alarm.lnk"))
lnk.TargetPath = "wscript.exe"
lnk.Arguments = """" & vbsLauncher & """"
lnk.WorkingDirectory = scriptDir
lnk.Description = "AGSHome Alarm Server"
lnk.IconLocation = "shell32.dll,47"
lnk.Save

' Start Menu shortcut (appears in Windows search)
startMenu = WshShell.SpecialFolders("Programs")
Set lnk2 = WshShell.CreateShortcut(FSO.BuildPath(startMenu, "AGSHome Alarm.lnk"))
lnk2.TargetPath = "wscript.exe"
lnk2.Arguments = """" & vbsLauncher & """"
lnk2.WorkingDirectory = scriptDir
lnk2.Description = "AGSHome Alarm Server"
lnk2.IconLocation = "shell32.dll,47"
lnk2.Save

MsgBox "AGSHome shortcuts created:" & vbCrLf & vbCrLf & _
       "  - Desktop" & vbCrLf & _
       "  - Start Menu (searchable)", vbInformation, "AGSHome"
