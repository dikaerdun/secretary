[CmdletBinding()]
param([ValidateRange(1024, 65535)][int]$Port = 8766, [switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskScript = Join-Path $PSScriptRoot 'training.py'
$taskStateFile = Join-Path $PSScriptRoot 'training-process.local.json'
$taskDatabase = Join-Path $taskRoot 'data\training-secretary.sqlite3'
$taskUrl = "http://127.0.0.1:$Port"
if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) { throw '请先按 README 安装 .venv，再启动离线演练。' }
$taskBasePython = ([string](& $taskPython -c 'import os, sys; print(os.path.realpath(sys._base_executable))')).Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $taskBasePython -PathType Leaf)) { throw '无法确定虚拟环境 Python 解释器。' }

function Get-TrainingProcess($taskState) {
    if ($taskState.module -ne 'deploy.training' -or $taskState.app_root -ne $taskRoot -or
        $taskState.executable -ne $taskPython -or $taskState.process_executable -ne $taskBasePython -or
        $taskState.script -ne $taskScript -or $taskState.database -ne $taskDatabase -or
        $taskState.instance_id -notmatch '^[a-f0-9]{32}$') { throw '演练进程记录的身份不匹配；不会覆盖或停止未知进程。' }
    $taskProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$taskState.pid)" -ErrorAction SilentlyContinue
    if ($null -eq $taskProcess) { return $null }
    $taskCommand = [string]$taskProcess.CommandLine
    if ($taskProcess.ExecutablePath -ne $taskBasePython -or -not $taskCommand.Contains($taskPython) -or
        -not $taskCommand.Contains($taskScript) -or -not $taskCommand.Contains($taskStateFile) -or
        $taskCommand -notmatch ('(?:^|\s)--instance-id\s+' + [regex]::Escape($taskState.instance_id) + '(?:\s|$)')) {
        throw '该 PID 当前不属于演练实例；不会覆盖记录或停止其他进程。'
    }
    return $taskProcess
}
if (Test-Path -LiteralPath $taskStateFile -PathType Leaf) {
    try { $taskPrior = Get-Content -LiteralPath $taskStateFile -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw '演练进程记录损坏，请保留并检查。' }
    if ($taskPrior.status -eq 'ready' -and $null -ne (Get-TrainingProcess $taskPrior)) {
        if ([int]$taskPrior.port -ne $Port) { throw '演练已在其他端口运行；请先运行 stop-training.ps1。' }
        Write-Host "离线演练已运行：$($taskPrior.url)"
        Write-Host '公开演练口令：learn-secretary-2026；资料均为虚构。'
        if (-not $NoBrowser) { Start-Process -FilePath $taskPrior.url }
        return
    }
    if ($taskPrior.status -notin @('ready','stopped')) { throw '未知演练实例状态，请保留进程记录并检查。' }
    # A stopped record is retained. A stale ready record must still pass identity
    # verification above; PID reuse never authorizes stopping its new occupant.
}
if ($null -ne (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) {
    throw '演练端口被其他服务占用；不会停止其他服务。可用 -Port 选择空闲端口。'
}
$taskInstance = [guid]::NewGuid().ToString('N')
$taskStopFile = Join-Path $PSScriptRoot "training-stop-$taskInstance.local"
function Quote-TrainingArgument([string]$taskValue) {
    if ($taskValue.Contains('"') -or $taskValue.EndsWith('\')) { throw '启动路径含有不支持的字符。' }
    return '"' + $taskValue + '"'
}
$taskArguments = @((Quote-TrainingArgument $taskScript), '--port', "$Port", '--state-file',
    (Quote-TrainingArgument $taskStateFile), '--stop-file', (Quote-TrainingArgument $taskStopFile), '--instance-id', $taskInstance)
$taskChild = Start-Process -FilePath $taskPython -ArgumentList $taskArguments -WorkingDirectory $taskRoot `
    -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot 'training-stdout.local') `
    -RedirectStandardError (Join-Path $PSScriptRoot 'training-stderr.local') -PassThru
$taskDeadline = [DateTime]::UtcNow.AddSeconds(35)
$taskReady = $false
while ([DateTime]::UtcNow -lt $taskDeadline) {
    if (Test-Path -LiteralPath $taskStateFile -PathType Leaf) {
        try {
            $taskCurrent = Get-Content -LiteralPath $taskStateFile -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($taskCurrent.instance_id -eq $taskInstance -and $taskCurrent.status -eq 'ready' -and
                $null -ne (Get-TrainingProcess $taskCurrent)) { $taskReady = $true; break }
        } catch { }
    }
    $taskChild.Refresh()
    if ($taskChild.HasExited) { throw '演练启动失败，请检查 deploy/training-stderr.local；训练资料保留。' }
    Start-Sleep -Milliseconds 250
}
if (-not $taskReady) { throw '演练未在时限内确认启动；请检查训练日志与进程记录。不会停止未知进程。' }
Write-Host "离线演练已启动：$taskUrl"
Write-Host '公开演练口令：learn-secretary-2026；仅离线示例输出，无真实 AI/ASR。'
Write-Host '训练资料持久保存在 data/training-secretary.sqlite3；企微、聆记和网络提醒均未连接。'
if (-not $NoBrowser) { Start-Process -FilePath $taskUrl }
