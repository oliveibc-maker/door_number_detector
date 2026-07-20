$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$python = "$root\.venv310\Scripts\python.exe"
Set-Location $root

$bg = "False"
Get-Content "$root\.env" | ForEach-Object {
    if ($_ -match "^\s*RUN_IN_BACKGROUND\s*=\s*(.+)$") {
        $bg = $Matches[1].Trim()
    }
}

if ($bg -eq "True") {
    Write-Host "Starting server in background..." -ForegroundColor Cyan
    $proc = Start-Process `
        -FilePath $python `
        -ArgumentList "`"$root\web\app.py`"" `
        -WorkingDirectory $root `
        -WindowStyle Hidden `
        -RedirectStandardOutput "$root\server.log" `
        -RedirectStandardError  "$root\server_error.log" `
        -PassThru
    Start-Sleep -Seconds 3
    if (-not $proc.HasExited) {
        Write-Host "Server running (PID $($proc.Id)). Access at http://127.0.0.1:8080" -ForegroundColor Green
        $proc.Id | Out-File "$root\server.pid" -Encoding ascii
    } else {
        Write-Host "Server failed to start — check server_error.log" -ForegroundColor Red
        Get-Content "$root\server_error.log" | Select-Object -Last 20
    }
} else {
    Write-Host "Starting server in foreground..." -ForegroundColor Cyan
    & $python "$root\web\app.py"
}
