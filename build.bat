@echo off
rem Trae 自动签到 - 打包成单文件 exe
setlocal
cd /d "%~dp0"

python -m PyInstaller --onefile --console --name trae-checkin --noconfirm main.py
if errorlevel 1 (
    echo 打包失败
    exit /b 1
)

echo.
echo 打包完成: dist\trae-checkin.exe
echo   dist\trae-checkin.exe            常驻后台自动签到
echo   dist\trae-checkin.exe --status   查看今日状态与记录
echo   dist\trae-checkin.exe --install  注册开机自启
endlocal
