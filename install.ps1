# Windows entry point. Supports Windows PowerShell 5.1 and PowerShell 7.
$ErrorActionPreference = 'Stop'
$learnCandidates = @()
if ($env:CODEX_LEARN_PYTHON) {
    $learnCandidates += @{ Path = $env:CODEX_LEARN_PYTHON; Prefix = @() }
}
$learnBundled = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
if (Test-Path -LiteralPath $learnBundled) {
    $learnCandidates += @{ Path = $learnBundled; Prefix = @() }
}
foreach ($learnName in @('python3', 'python', 'py')) {
    $learnCommand = Get-Command $learnName -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($learnCommand -and $learnCommand.Source -notmatch 'WindowsApps') {
        $learnPrefix = @()
        if ($learnName -eq 'py') { $learnPrefix = @('-3') }
        $learnCandidates += @{ Path = $learnCommand.Source; Prefix = $learnPrefix }
    }
}
foreach ($learnCandidate in $learnCandidates) {
    $learnExecutable = $learnCandidate.Path
    $learnPrefix = $learnCandidate.Prefix
    if (-not (Test-Path -LiteralPath $learnExecutable)) { continue }
    & $learnExecutable @learnPrefix -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null
    if ($LASTEXITCODE -ne 0) { continue }
    & $learnExecutable @learnPrefix (Join-Path $PSScriptRoot 'install.py') @args
    exit $LASTEXITCODE
}
Write-Error 'Python 3.10+ is required. Install Python or set CODEX_LEARN_PYTHON to its executable.'
exit 1
