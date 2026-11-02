# Dedicated native Windows entry; stdin remains the child's original byte stream.
$ErrorActionPreference = 'Stop'
try {
    $env:NoDefaultCurrentDirectoryInExePath = '1'
    $env:PATH = (($env:PATH -split ';' | Where-Object { $_ -match '^(?:[A-Za-z]:[\\/]|\\\\)' }) -join ';')
    $cvScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'context_vault/codex_entry.py'
    foreach ($cvName in @('py', 'python3', 'python')) {
        $cvExe = Get-Command $cvName -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cvExe) {
            & $cvExe.Source $cvScript
            exit 0
        }
    }
} catch {
    [Console]::Error.WriteLine('[context-vault] Codex hook unavailable')
}
exit 0
