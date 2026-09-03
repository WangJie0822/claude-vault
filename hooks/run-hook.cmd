: << 'BATCH'
@echo off
setlocal
rem Pick the first candidate root that actually CONTAINS the script, not the
rem first non-empty variable. PLUGIN_ROOT is a very generic name; any unrelated
rem tool exporting it used to win over the host's own CLAUDE_PLUGIN_ROOT and,
rem because the old code never fell back, the plugin went silently dead.
rem NOTE: keep this batch section pure ASCII. cmd.exe reads .cmd in the OEM
rem codepage, so non-ASCII comments get mojibaked, executed as commands, and
rem even break "@echo off".
set "SCRIPT="
if not "%PLUGIN_ROOT%"=="" if exist "%PLUGIN_ROOT%\%~1" set "SCRIPT=%PLUGIN_ROOT%\%~1"
if "%SCRIPT%"=="" if not "%CLAUDE_PLUGIN_ROOT%"=="" if exist "%CLAUDE_PLUGIN_ROOT%\%~1" set "SCRIPT=%CLAUDE_PLUGIN_ROOT%\%~1"
if "%SCRIPT%"=="" if exist "%~dp0..\%~1" set "SCRIPT=%~dp0..\%~1"
if "%SCRIPT%"=="" (
  echo [context-vault] hook script not found: %~1 ^(tried PLUGIN_ROOT, CLAUDE_PLUGIN_ROOT, wrapper dir^) 1>&2
  exit /b 0
)
where py >nul 2>&1 && ( py "%SCRIPT%" & exit /b 0 )
where python3 >nul 2>&1 && ( python3 "%SCRIPT%" & exit /b 0 )
where python >nul 2>&1 && ( python "%SCRIPT%" & exit /b 0 )
exit /b 0
BATCH
# Same candidate-root rule as the batch section above.
SELF_ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
SCRIPT=""
for CAND in "$PLUGIN_ROOT" "$CLAUDE_PLUGIN_ROOT" "$SELF_ROOT"; do
  [ -n "$CAND" ] || continue
  if [ -f "$CAND/$1" ]; then SCRIPT="$CAND/$1"; break; fi
done
if [ -z "$SCRIPT" ]; then
  echo "[context-vault] hook script not found: $1 (tried PLUGIN_ROOT, CLAUDE_PLUGIN_ROOT, wrapper dir)" >&2
  exit 0
fi
if command -v py >/dev/null 2>&1; then PY=py
elif command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else exit 0
fi
"$PY" "$SCRIPT"
exit 0
