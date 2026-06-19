$PythonW = "C:\Users\steve\AppData\Local\Programs\Python\Python313\pythonw.exe"
$AresDir = "C:\Ares"
$TaskFolder = "\Ares\"

$Tasks = @(
    @{ Name="Ares_Hamilton";        Args="hamilton_filter.py";               Hour=8;  Minute=45 },
    @{ Name="Ares_EngineA_Scan";    Args="engine_a.py --scan";               Hour=9;  Minute=35 },
    @{ Name="Ares_EngineB_Scan";    Args="engine_b.py --scan";               Hour=9;  Minute=35 },
    @{ Name="Ares_EngineC_Scan";    Args="engine_c.py --scan";               Hour=9;  Minute=36 },
    @{ Name="Ares_EngineE_Scan";    Args="engine_e.py --scan";               Hour=9;  Minute=37 },
    @{ Name="Ares_EngineF_Scan";    Args="engine_f.py --scan";               Hour=9;  Minute=38 },
    @{ Name="Ares_ExitMonitor_AM";  Args="ares_exit_monitor.py";             Hour=9;  Minute=50 },
    @{ Name="Ares_HoldMonitor_AM";  Args="ares_hold_monitor.py";             Hour=9;  Minute=52 },
    @{ Name="Ares_ExitMonitor_PM";  Args="ares_exit_monitor.py";             Hour=15; Minute=55 },
    @{ Name="Ares_HoldMonitor_PM";  Args="ares_hold_monitor.py";             Hour=15; Minute=58 },
    @{ Name="Ares_OutcomeTracker";  Args="outcome_tracker.py --all-forward"; Hour=16; Minute=15 },
    @{ Name="Ares_DailyRecap";      Args="daily_recap.py";                   Hour=16; Minute=20 }
)

Write-Host "`n=== Ares Task Scheduler Setup ===" -ForegroundColor Cyan
Write-Host "Python: $PythonW"
Write-Host "Start in: $AresDir"
Write-Host ""

if (-not (Test-Path $PythonW)) {
    Write-Host "ERROR: pythonw.exe not found" -ForegroundColor Red
    exit 1
}

foreach ($t in $Tasks) {
    $existing = Get-ScheduledTask -TaskPath $TaskFolder -TaskName $t.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "EXISTS  $($t.Name) @ $($t.Hour):$($t.Minute.ToString('D2'))" -ForegroundColor Yellow
    } else {
        try {
            $action   = New-ScheduledTaskAction -Execute $PythonW -Argument $t.Args -WorkingDirectory $AresDir
            $trigger  = New-ScheduledTaskTrigger -Daily -At "$($t.Hour):$($t.Minute.ToString('D2'))"
            $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
            Register-ScheduledTask `
                -TaskName $t.Name `
                -TaskPath $TaskFolder `
                -Action $action `
                -Trigger $trigger `
                -Settings $settings `
                -Description "Ares: $($t.Args)" | Out-Null
            Write-Host "CREATED $($t.Name) @ $($t.Hour):$($t.Minute.ToString('D2'))" -ForegroundColor Green
        } catch {
            Write-Host "FAILED  $($t.Name): $_" -ForegroundColor Red
        }
    }
}

Write-Host ""
Write-Host "=== Current \Ares\ task status ===" -ForegroundColor Cyan
Get-ScheduledTask -TaskPath $TaskFolder | Select-Object TaskName, State | Format-Table -AutoSize
