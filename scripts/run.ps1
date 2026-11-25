# Use Python 3, including the Python bundled with the Codex app.
$ErrorActionPreference = 'Stop'
$learnPython = $null
$learnPrefix = @()
if ($env:CODEX_LEARN_PYTHON -and (Test-Path -LiteralPath $env:CODEX_LEARN_PYTHON)) {
    $learnPython = $env:CODEX_LEARN_PYTHON
}
if (-not $learnPython) {
    $learnBundled = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
    if (Test-Path -LiteralPath $learnBundled) { $learnPython = $learnBundled }
}
if (-not $learnPython) {
    foreach ($learnName in @('python3', 'python', 'py')) {
        $learnCommand = Get-Command $learnName -ErrorAction SilentlyContinue
        if ($learnCommand -and $learnCommand.Source -notmatch 'WindowsApps') {
            $learnPython = $learnCommand.Source
            if ($learnName -eq 'py') { $learnPrefix = @('-3') }
            break
        }
    }
}
if (-not $learnPython) { throw 'Python 3 is required. Set CODEX_LEARN_PYTHON to its executable path.' }
& $learnPython @learnPrefix -X utf8 (Join-Path $PSScriptRoot 'study.py') @args
exit $LASTEXITCODE
