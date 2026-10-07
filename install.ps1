# Sprint Glance installer for Windows: find a Python 3.9+ with Tk, then run setup.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$check = 'import sys, tkinter; sys.exit(sys.version_info < (3, 9) or tkinter.TkVersion < 8.6)'

function Test-Python($exe, $prefix) {
    try {
        & $exe @prefix -c $check 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

# The py launcher comes with the python.org installer. Plain "python" may be the
# Microsoft Store stub, which fails the check rather than running anything.
$candidates = @(
    @{ exe = 'py';      prefix = @('-3') },
    @{ exe = 'python';  prefix = @() },
    @{ exe = 'python3'; prefix = @() }
)
foreach ($c in $candidates) {
    if ((Get-Command $c.exe -ErrorAction SilentlyContinue) -and (Test-Python $c.exe $c.prefix)) {
        & $c.exe @($c.prefix) sprint_glance.py setup
        exit $LASTEXITCODE
    }
}

Write-Host @'
No Python with Tk found. Install one, then run install.cmd again:
  - winget install Python.Python.3.12
  - or the Windows installer from https://www.python.org/downloads/
    (leave "tcl/tk and IDLE" ticked, which is the default)
'@
exit 1
