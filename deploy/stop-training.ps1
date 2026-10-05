[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskScript = Join-Path $PSScriptRoot 'training.py'
$taskStateFile = Join-Path $PSScriptRoot 'training-process.local.json'
$taskDatabase = Join-Path $taskRoot 'data\training-secretary.sqlite3'
if (-not (Test-Path -LiteralPath $taskStateFile -PathType Leaf)) { Write-Host '没有演练进程记录；未停止其他进程。'; return }
try { $taskState = Get-Content -LiteralPath $taskStateFile -Raw -Encoding UTF8 | ConvertFrom-Json }
catch { throw '演练进程记录损坏；未停止任何进程。' }
if ($taskState.status -eq 'stopped') { Write-Host '离线演练已停止；训练资料保留。'; return }
$taskBasePython = ([string](& $taskPython -c 'import os, sys; print(os.path.realpath(sys._base_executable))')).Trim()
if ($LASTEXITCODE -ne 0 -or $taskState.module -ne 'deploy.training' -or $taskState.app_root -ne $taskRoot -or
    $taskState.executable -ne $taskPython -or $taskState.process_executable -ne $taskBasePython -or
    $taskState.script -ne $taskScript -or $taskState.database -ne $taskDatabase -or
    $taskState.instance_id -notmatch '^[a-f0-9]{32}$') { throw '演练进程身份不匹配；未停止任何进程。' }
$taskExpectedStop = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "training-stop-$($taskState.instance_id).local"))
if ($taskState.stop_file -ne $taskExpectedStop) { throw '演练停止文件路径不匹配；未停止任何进程。' }
function Get-VerifiedTrainingProcess {
    $taskProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$taskState.pid)" -ErrorAction SilentlyContinue
    if ($null -eq $taskProcess) { return $null }
    $taskCommand = [string]$taskProcess.CommandLine
    if ($taskProcess.ExecutablePath -ne $taskBasePython -or -not $taskCommand.Contains($taskPython) -or
        -not $taskCommand.Contains($taskScript) -or -not $taskCommand.Contains($taskStateFile) -or
        $taskCommand -notmatch ('(?:^|\s)--instance-id\s+' + [regex]::Escape($taskState.instance_id) + '(?:\s|$)')) {
        throw '该 PID 当前不属于记录中的演练实例；未停止其他进程。'
    }
    return $taskProcess
}
if ($null -eq (Get-VerifiedTrainingProcess)) { Write-Host '演练进程已退出；训练资料及记录保留。'; return }
@{ instance_id = $taskState.instance_id } | ConvertTo-Json -Compress | Set-Content -LiteralPath $taskExpectedStop -Encoding UTF8
$taskDeadline = [DateTime]::UtcNow.AddSeconds(20)
while ([DateTime]::UtcNow -lt $taskDeadline) {
    try { $taskProcess = Get-VerifiedTrainingProcess }
    catch {
        $taskCurrent = Get-Content -LiteralPath $taskStateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($taskCurrent.instance_id -eq $taskState.instance_id -and $taskCurrent.status -eq 'stopped') {
            Write-Host '离线演练已停止；训练资料保留，重启可继续练习。'; return
        }
        Start-Sleep -Milliseconds 250; continue
    }
    if ($null -eq $taskProcess) { Write-Host '离线演练已停止；训练资料保留，重启可继续练习。'; return }
    Start-Sleep -Milliseconds 250
}
# No forced termination: preserve SQLite writes and refuse to touch any unknown PID.
throw '演练未及时退出；停止请求已保留。请检查训练日志与进程状态，未强制结束任何进程。'
