@echo off
echo Installing PyInstaller and dependencies...
python -m pip install pyinstaller Pillow numpy

echo.
echo Building executable...
python -m PyInstaller build_exe.spec

echo.
set "INSTALL_DIR=C:\Users\Mouad\.antigravity_video_player"
echo Installing to %INSTALL_DIR%...

if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

echo Copying files...
if exist "dist\AntigravityVideoPlayer" xcopy /E /I /Y "dist\AntigravityVideoPlayer" "%INSTALL_DIR%"
if not exist "dist\AntigravityVideoPlayer" copy /Y "dist\AntigravityVideoPlayer.exe" "%INSTALL_DIR%\"
copy /Y "My project.exe" "%INSTALL_DIR%\"
copy /Y "UnityPlayer.dll" "%INSTALL_DIR%\"
copy /Y "UnityCrashHandler64.exe" "%INSTALL_DIR%\"

echo Copying folders...
if exist "My project_Data" xcopy /E /I /Y "My project_Data" "%INSTALL_DIR%\My project_Data"
if exist "MonoBleedingEdge" xcopy /E /I /Y "MonoBleedingEdge" "%INSTALL_DIR%\MonoBleedingEdge"
if exist "D3D12" xcopy /E /I /Y "D3D12" "%INSTALL_DIR%\D3D12"

echo.
echo Creating Desktop shortcut...
set "SHORTCUT_PATH=%USERPROFILE%\Desktop\Antigravity Video Player.lnk"
set "TARGET_PATH=%INSTALL_DIR%\AntigravityVideoPlayer.exe"

powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT_PATH%'); $s.TargetPath = '%TARGET_PATH%'; $s.WorkingDirectory = '%INSTALL_DIR%'; $s.Save()"

echo.
echo Done! Installed to %INSTALL_DIR% and shortcut created on Desktop.
pause
