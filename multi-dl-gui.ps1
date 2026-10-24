# MULTI_DOWNLOADER Desktop GUI PowerShell Launcher
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ScriptDir ".venv\Scripts\python.exe"
$PythonBin = if (Test-Path $VenvPython) { $VenvPython } else { "python" }
$GuiScript = Join-Path $ScriptDir "multi-dl-gui.py"

Start-Process -FilePath $PythonBin -ArgumentList "`"$GuiScript`""
