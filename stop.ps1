$root = "C:\Users\mafapereira\OneDrive - NOS SGPS, S.A\Documents\door_number_detector_new"
$pidFile = "$root\server.pid"

Write-Host "Stopping Door Number Detector..." -ForegroundColor Cyan

if (Test-Path $pidFile) {
    $serverPid = Get-Content $pidFile
    try {
        Stop-Process -Id $serverPid -Force -ErrorAction Stop
        Write-Host "Stopped PID $serverPid." -ForegroundColor Green
    } catch {
        Write-Host "PID $serverPid not found — may have already stopped." -ForegroundColor Yellow
    }
    Remove-Item $pidFile -Force
} else {
    $procs = Get-WmiObject Win32_Process | Where-Object { $_.CommandLine -like "*app.py*" }
    if ($procs) {
        $procs | ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force
            Write-Host "Stopped PID $($_.ProcessId)." -ForegroundColor Green
        }
    } else {
        Write-Host "No running server found." -ForegroundColor Yellow
    }
}
