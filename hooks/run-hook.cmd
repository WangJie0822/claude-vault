: << 'BATCH'
@echo off
setlocal
rem Pick the first candidate root that actually CONTAINS the script, not the
rem first non-empty variable. Candidates: CLAUDE_PLUGIN_ROOT, then the plugin
rem root this wrapper lives in. PLUGIN_ROOT is deliberately NOT a candidate
rem since 2.0.0: it is a generic name any tool may export, and no supported
rem host sets it for this plugin any more.
rem NOTE: keep this batch section pure ASCII. cmd.exe reads .cmd in the OEM
rem codepage, so non-ASCII comments get mojibaked, executed as commands, and
rem even break "@echo off".
set "SCRIPT="
if not "%CLAUDE_PLUGIN_ROOT%"=="" if exist "%CLAUDE_PLUGIN_ROOT%\%~1" set "SCRIPT=%CLAUDE_PLUGIN_ROOT%\%~1"
if "%SCRIPT%"=="" if exist "%~dp0..\%~1" set "SCRIPT=%~dp0..\%~1"
if "%SCRIPT%"=="" (
  echo [context-vault] hook script not found: %~1 ^(tried CLAUDE_PLUGIN_ROOT, wrapper dir^) 1>&2
  exit /b 0
)
where py >nul 2>&1 && ( py "%SCRIPT%" & exit /b 0 )
where python3 >nul 2>&1 && ( python3 "%SCRIPT%" & exit /b 0 )
where python >nul 2>&1 && ( python "%SCRIPT%" & exit /b 0 )
exit /b 0
BATCH
# Same candidate-root rule as the batch section above (PLUGIN_ROOT is not a candidate).
SELF_ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
SCRIPT=""
for CAND in "$CLAUDE_PLUGIN_ROOT" "$SELF_ROOT"; do
  [ -n "$CAND" ] || continue
  if [ -f "$CAND/$1" ]; then SCRIPT="$CAND/$1"; break; fi
done
if [ -z "$SCRIPT" ]; then
  echo "[context-vault] hook script not found: $1 (tried CLAUDE_PLUGIN_ROOT, wrapper dir)" >&2
  exit 0
fi
if command -v py >/dev/null 2>&1; then PY=py
elif command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else exit 0
fi
"$PY" "$SCRIPT"
exit 0
