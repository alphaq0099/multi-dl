# MULTI_DOWNLOADER PowerShell Launcher
# Ensures execution stays inside the current terminal using the virtual environment
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ScriptDir ".venv\Scripts\python.exe"
$PythonBin = if (Test-Path $VenvPython) { $VenvPython } else { "python" }
$MainScript = Join-Path $ScriptDir "multi-dl.py"

& $PythonBin $MainScript @args

exit $LASTEXITCODE
