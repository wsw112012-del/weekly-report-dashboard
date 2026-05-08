@echo off
set CLOUDFLARED=C:\Users\쿠콘_우승우\AppData\Local\Microsoft\WinGet\Packages\Cloudflare.cloudflared_Microsoft.Winget.Source_8wekyb3d8bbwe\cloudflared.exe

echo [1] FastAPI 서버 시작...
start "주간보고 서버" cmd /k "cd /d %~dp0 && python app.py"

timeout /t 5 /nobreak >nul

echo [2] Cloudflare Tunnel 시작...
start "Cloudflare Tunnel" cmd /k "%CLOUDFLARED% tunnel --url http://localhost:8765"

echo.
echo 서버가 시작됐습니다.
echo Cloudflare Tunnel URL은 "Cloudflare Tunnel" 창에서 확인하세요.
