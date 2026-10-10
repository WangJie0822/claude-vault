: << 'BATCH'
@echo off
setlocal
rem Hosts run this file through /bin/sh (hooks/hooks.json); this batch section
rem only runs when someone executes the wrapper directly with cmd.exe.
rem Never resolve py / python3 / python from the current directory: a cloned
rem repo could plant py.bat there (CWE-427). cmd.exe and CreateProcess honor
rem this variable; where.exe does not, hence "where $PATH:" below. An explicit
rem "." in PATH is still honored here.
set "NoDefaultCurrentDirectoryInExePath=1"
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
where $PATH:py >nul 2>&1 && ( py "%SCRIPT%" & exit /b 0 )
where $PATH:python3 >nul 2>&1 && ( python3 "%SCRIPT%" & exit /b 0 )
where $PATH:python >nul 2>&1 && ( python "%SCRIPT%" & exit /b 0 )
exit /b 0
BATCH
# Same candidate-root rule as the batch section above (PLUGIN_ROOT is not a candidate).
# Child processes must not resolve program names from the current directory on
# Windows (Git Bash runs this section there via hooks.json); no effect on POSIX.
export NoDefaultCurrentDirectoryInExePath=1
# Keep only absolute PATH entries before resolving the interpreter: an empty entry,
# "." or a relative entry means the current directory (an untrusted repo), and "py"
# is normally absent on POSIX, so a planted ./py would win from any position.
# Exported, so the Python script and its own children (git, claude) inherit it.
_cv_path=; _cv_ifs=$IFS; IFS=:; set -f
for _cv_dir in $PATH; do
  case $_cv_dir in /*) _cv_path=${_cv_path:+$_cv_path:}$_cv_dir ;; esac
done
set +f; IFS=$_cv_ifs
PATH=${_cv_path:-/usr/bin:/bin}; export PATH
SCRIPT=""
if [ -n "$CLAUDE_PLUGIN_ROOT" ] && [ -f "$CLAUDE_PLUGIN_ROOT/$1" ]; then
  SCRIPT="$CLAUDE_PLUGIN_ROOT/$1"
else
  # Only computed when the first candidate misses: the two subshells plus dirname
  # cost ~40ms per hook on Git Bash, and Claude Code normally sets the variable.
  SELF_ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
  if [ -n "$SELF_ROOT" ] && [ -f "$SELF_ROOT/$1" ]; then SCRIPT="$SELF_ROOT/$1"; fi
fi
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
