: << 'BATCH'
@echo off
setlocal
if "%CLAUDE_PLUGIN_ROOT%"=="" (set "ROOT=%~dp0..") else (set "ROOT=%CLAUDE_PLUGIN_ROOT%")
set "SCRIPT=%ROOT%\%~1"
if not exist "%SCRIPT%" exit /b 0
where py >nul 2>&1 && ( py "%SCRIPT%" & exit /b 0 )
where python3 >nul 2>&1 && ( python3 "%SCRIPT%" & exit /b 0 )
where python >nul 2>&1 && ( python "%SCRIPT%" & exit /b 0 )
exit /b 0
BATCH
ROOT="${CLAUDE_PLUGIN_ROOT:-$(CDPATH= cd "$(dirname "$0")/.." && pwd)}"
SCRIPT="$ROOT/$1"
[ -f "$SCRIPT" ] || exit 0
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else exit 0
fi
"$PY" "$SCRIPT"
exit 0
