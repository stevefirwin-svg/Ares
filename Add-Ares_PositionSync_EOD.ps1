# Add-Ares_PositionSync_EOD.ps1
# Run as Administrator in PowerShell from C:\Ares

$PythonW  = "C:\Users\steve\AppData\Local\Programs\Python\Python313\pythonw.exe"
$AresDir  = "C:\Ares"
$TaskPath = "\Ares\"
$TaskName = "Ares_PositionSync_EOD"

if (-not (Test-Path $PythonW)) {
    Write-Host "ERROR: pythonw.exe not found at $PythonW" -ForegroundColor Red
    exit 1
}

$existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing $TaskName" -ForegroundColor Yellow
}

$action  = New-ScheduledTaskAction `
               -Execute $PythonW `
               -Argument "ares_position_sync.py --void-ghosts" `
               -WorkingDirectory $AresDir

$trigger = New-ScheduledTaskTrigger -Daily -At "16:05"

Register-ScheduledTask `
    -TaskName    $TaskName `
    -TaskPath    $TaskPath `
    -Action      $action `
    -Trigger     $trigger `
    -Description "Ares EOD: void unfilled ledger entries before daily recap" | Out-Null

Write-Host ""
Write-Host "Created: $TaskName @ 4:05 PM daily" -ForegroundColor Green
Write-Host ""
Write-Host "=== Current \Ares\ tasks ===" -ForegroundColor Cyan
Get-ScheduledTask -TaskPath $TaskPath | Select-Object TaskName, State | Format-Table -AutoSize
