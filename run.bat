@echo off
cd /d "%~dp0"
if not exist config.json (
  echo Creating config.json from config.example.json ...
  copy /Y config.example.json config.json >nul
  echo Please edit config.json before registering.
)
set MIRASIM_REG_PORT=8788
echo Open http://127.0.0.1:%MIRASIM_REG_PORT%/
python -m uvicorn server:app --host 127.0.0.1 --port %MIRASIM_REG_PORT%
