# ScreenPixelator 1.0 installer
# Installs Python 3.13 (if needed) and the packages, copies the program to
# %LOCALAPPDATA%\ScreenPixelator, makes shortcuts and starts it.

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"      # much faster downloads
$Src = Split-Path -Parent $MyInvocation.MyCommand.Path
$App = Join-Path $env:LOCALAPPDATA "ScreenPixelator"

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host ""; Write-Host "ERROR: $text" -ForegroundColor Red; exit 1 }

function Find-Python {
    $found = @()
    foreach ($launcher in @("py", "$env:LOCALAPPDATA\Programs\Python\Launcher\py.exe", "$env:WINDIR\py.exe")) {
        try {
            $out = & $launcher -3.13 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) { $found += ([string]($out | Select-Object -Last 1)).Trim() }
        } catch {}
    }
    $found += "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"
    $found += "$env:ProgramFiles\Python313\python.exe"
    foreach ($p in $found) { if ($p -and (Test-Path $p)) { return $p } }
    return $null
}

function New-Shortcut($Path, $Target, $Arguments) {
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($Path)
    $s.TargetPath = $Target
    $s.Arguments = $Arguments
    $s.WorkingDirectory = $App
    $s.IconLocation = (Join-Path $App "pixelator.ico") + ",0"
    $s.Save()
}

Write-Host "ScreenPixelator 1.0 installer" -ForegroundColor Green

# --- Python ---
Step "Python 3.13"
$py = Find-Python
if ($py) {
    Write-Host "Found: $py"
} else {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "Installing Python 3.13 with winget..."
        winget install --exact --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements
        $py = Find-Python
    }
    if (-not $py) {
        Write-Host "Downloading Python 3.13 from python.org..."
        try {
            $page = (Invoke-WebRequest "https://www.python.org/downloads/windows/" -UseBasicParsing).Content
            $nums = [regex]::Matches($page, "python-3\.13\.(\d+)-amd64\.exe") |
                    ForEach-Object { [int]$_.Groups[1].Value } | Sort-Object -Descending -Unique
        } catch { $nums = @() }
        if (-not $nums) { $nums = @(14) }
        $n = @($nums)[0]
        $exe = Join-Path $env:TEMP "python-3.13.$n-amd64.exe"
        try {
            Invoke-WebRequest "https://www.python.org/ftp/python/3.13.$n/python-3.13.$n-amd64.exe" -OutFile $exe -UseBasicParsing
        } catch { Fail "Could not download Python: $_" }
        Write-Host "Installing Python 3.13.$n..."
        Start-Process $exe -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_tcltk=1 Include_pip=1" -Wait
        $py = Find-Python
    }
    if (-not $py) { Fail "Python 3.13 could not be installed. Install it from python.org, then run this installer again." }
    Write-Host "Installed: $py"
}
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { Fail "pythonw.exe not found next to $py" }

# --- Packages ---
Step "Python packages (nudenet, onnxruntime, opencv, mss, dxcam)"
& $py -m pip install --upgrade --disable-pip-version-check pip
& $py -m pip install --upgrade --disable-pip-version-check nudenet onnxruntime numpy mss dxcam
if ($LASTEXITCODE -ne 0) { Fail "Installing the Python packages failed (see the messages above)." }
& $py -c "import nudenet, onnxruntime, cv2, mss, numpy; cv2.DISOpticalFlow_create(0); print('Packages OK')"
if ($LASTEXITCODE -ne 0) { Fail "The packages were installed but do not load (see above)." }
& $py -c "import tkinter" 2>$null
if ($LASTEXITCODE -ne 0) {
    Fail "Python has no Tk (needed for the settings window). Run the Python installer again, choose Modify and tick 'tcl/tk and IDLE'."
}

# --- Stop a running pixelator (this one, or an older copy started by hand) ---
Step "Stopping a running pixelator, if any"
if (Test-Path (Join-Path $App "running.pid")) {
    New-Item -ItemType File -Force -Path (Join-Path $App "stop.request") | Out-Null
    Start-Sleep -Seconds 3
}
$mains = Get-CimInstance Win32_Process |
         Where-Object { $_.Name -like "python*" -and $_.CommandLine -like "*screen_pixelator.py*" }
foreach ($m in $mains) {
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$($m.ProcessId)" |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Stop-Process -Id $m.ProcessId -Force -ErrorAction SilentlyContinue
}

# --- Program files ---
Step "Copying the program to $App"
New-Item -ItemType Directory -Force -Path $App | Out-Null
foreach ($f in "screen_pixelator.py", "pixelator_settings.pyw", "yolo11n-seg.onnx", "pixelator.ico") {
    Copy-Item (Join-Path $Src $f) $App -Force
}
foreach ($f in "running.pid", "stop.request") { Remove-Item (Join-Path $App $f) -Force -ErrorAction SilentlyContinue }
$settings = Join-Path $App "settings.json"
if (-not (Test-Path $settings)) {
    Set-Content -Path $settings -Value '{ "run_at_startup": false }' -Encoding ASCII
}
$runAtStartup = $false
try { $runAtStartup = [bool]((Get-Content $settings -Raw | ConvertFrom-Json).run_at_startup -eq $true) } catch {}

# --- Shortcuts ---
Step "Shortcuts"
$settingsArgs = '"' + (Join-Path $App "pixelator_settings.pyw") + '"'
$pixelArgs = '"' + (Join-Path $App "screen_pixelator.py") + '"'
New-Shortcut (Join-Path ([Environment]::GetFolderPath("Programs")) "Screen Pixelator Settings.lnk") $pyw $settingsArgs
New-Shortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "Screen Pixelator Settings.lnk") $pyw $settingsArgs
$startup = Join-Path ([Environment]::GetFolderPath("Startup")) "Screen Pixelator.lnk"
if ($runAtStartup) { New-Shortcut $startup $pyw $pixelArgs } else { Remove-Item $startup -Force -ErrorAction SilentlyContinue }
Write-Host "Start menu and desktop: 'Screen Pixelator Settings'"
if ($runAtStartup) { Write-Host "Starts automatically when Windows starts." }

# --- Windows "Installed apps" entry (uninstall there works without the password) ---
$key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\ScreenPixelator"
New-Item -Path $key -Force | Out-Null
$values = @{
    DisplayName = "ScreenPixelator 1.0"; DisplayVersion = "1.0"; Publisher = "ScreenPixelator"
    DisplayIcon = (Join-Path $App "pixelator.ico"); InstallLocation = $App
    UninstallString = '"' + $pyw + '" "' + (Join-Path $App "pixelator_settings.pyw") + '" --uninstall'
}
foreach ($k in $values.Keys) { Set-ItemProperty -Path $key -Name $k -Value $values[$k] }
Set-ItemProperty -Path $key -Name NoModify -Value 1 -Type DWord
Set-ItemProperty -Path $key -Name NoRepair -Value 1 -Type DWord

# --- Start ---
Step "Starting"
Start-Process $pyw -ArgumentList $pixelArgs -WorkingDirectory $App
Start-Process $pyw -ArgumentList $settingsArgs -WorkingDirectory $App
Write-Host ""
Write-Host "Done. The settings window opens now; the pixelator runs in the background." -ForegroundColor Green
