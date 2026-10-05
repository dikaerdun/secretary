[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)][int]$Port = 8765,
    [string]$Database = 'data/local-secretary.sqlite3',
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskStateFile = Join-Path $PSScriptRoot 'local-process.local.json'
$taskAccessFile = Join-Path $PSScriptRoot 'local-access.local.json'
$taskUrl = "http://127.0.0.1:$Port"
$taskDatabase = if ([System.IO.Path]::IsPathRooted($Database)) {
    [System.IO.Path]::GetFullPath($Database)
} else { [System.IO.Path]::GetFullPath((Join-Path $taskRoot $Database)) }

if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) {
    throw '缺少 .venv\Scripts\python.exe。请先按仓库 README 安装环境，再启动本机后台。'
}
# Windows venv python.exe is a launcher. The serving child uses the base
# interpreter while its command line and sys.executable identify this venv.
$taskBasePython = ([string](& $taskPython -c 'import os, sys; print(os.path.realpath(sys._base_executable))')).Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $taskBasePython -PathType Leaf)) {
    throw '无法确定本机虚拟环境使用的 Python 解释器。'
}

function Get-ManagedLocalProcess($taskState) {
    if ($taskState.module -ne 'secretary.local' -or
        $taskState.app_root -ne $taskRoot -or $taskState.executable -ne $taskPython -or
        $taskState.process_executable -ne $taskBasePython -or
        $taskState.instance_id -notmatch '^[A-Za-z0-9]{1,64}$') { return $null }
    $taskProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$taskState.pid)" -ErrorAction SilentlyContinue
    if ($null -eq $taskProcess -or $taskProcess.ExecutablePath -ne $taskBasePython) { return $null }
    $taskCommand = [string]$taskProcess.CommandLine
    if (-not $taskCommand.Contains($taskPython) -or $taskCommand -notmatch '(?:^|\s)-m\s+secretary\.local(?:\s|$)' -or
        $taskCommand -notmatch ('(?:^|\s)--instance-id\s+' + [regex]::Escape($taskState.instance_id) + '(?:\s|$)') -or
        -not $taskCommand.Contains($taskStateFile)) { return $null }
    return $taskProcess
}

if (Test-Path -LiteralPath $taskStateFile -PathType Leaf) {
    try { $taskPrior = Get-Content -LiteralPath $taskStateFile -Raw | ConvertFrom-Json }
    catch { throw '本机进程记录文件损坏，请保留该文件并检查；启动脚本不会覆盖未知实例。' }
    if ($null -ne (Get-ManagedLocalProcess $taskPrior)) {
        if ([int]$taskPrior.port -ne $Port) { throw '已有本机后台在其他端口运行。请先运行 stop-local.ps1 再修改端口。' }
        if ($taskPrior.database -ne $taskDatabase) { throw '已有本机后台在使用另一份数据库。请先运行 stop-local.ps1 再修改数据库。' }
        Write-Host "本机后台已在运行：$($taskPrior.url)"
        Write-Host "登录口令文件：$taskAccessFile"
        if (-not $NoBrowser) { Start-Process -FilePath $taskPrior.url }
        return
    }
    # A stale PID never authorizes terminating its current occupant.
    Remove-Item -LiteralPath $taskStateFile
}

$taskListener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($null -ne $taskListener) { throw '本机端口已被其他服务占用。请使用 -Port 选择空闲端口；不会停止其他服务。' }

$taskInstance = [guid]::NewGuid().ToString('N')
$taskStopFile = Join-Path $PSScriptRoot "local-stop-$taskInstance.local"
$taskStdout = Join-Path $PSScriptRoot 'local-stdout.local'
$taskStderr = Join-Path $PSScriptRoot 'local-stderr.local'
function Quote-LocalArgument([string]$taskValue) {
    if ($taskValue.Contains('"') -or $taskValue.EndsWith('\')) { throw '启动路径中不能含有引号或尾部反斜杠。' }
    return '"' + $taskValue + '"'
}
$taskArguments = @('-m', 'secretary.local', '--port', "$Port", '--db', (Quote-LocalArgument $taskDatabase),
    '--access-file', (Quote-LocalArgument $taskAccessFile), '--state-file', (Quote-LocalArgument $taskStateFile),
    '--stop-file', (Quote-LocalArgument $taskStopFile), '--instance-id', $taskInstance)
$taskChild = Start-Process -FilePath $taskPython -ArgumentList $taskArguments -WorkingDirectory $taskRoot `
    -WindowStyle Hidden -RedirectStandardOutput $taskStdout -RedirectStandardError $taskStderr -PassThru

$taskDeadline = [DateTime]::UtcNow.AddSeconds(25)
$taskReady = $false
while ([DateTime]::UtcNow -lt $taskDeadline) {
    if (Test-Path -LiteralPath $taskStateFile -PathType Leaf) {
        try {
            $taskCurrent = Get-Content -LiteralPath $taskStateFile -Raw | ConvertFrom-Json
            if ($taskCurrent.instance_id -eq $taskInstance -and $taskCurrent.status -eq 'ready' -and
                $null -ne (Get-ManagedLocalProcess $taskCurrent)) { $taskReady = $true; break }
        } catch { }
    }
    $taskChild.Refresh()
    if ($taskChild.HasExited) { throw "本机后台启动失败。请检查 $taskStderr；日志不会包含密钥或材料正文。" }
    Start-Sleep -Milliseconds 250
}
if (-not $taskReady) { throw '本机后台未在时限内确认启动。请检查本机日志并运行 stop-local.ps1；不会结束未知进程。' }
Write-Host "本机后台已启动：$taskUrl"
Write-Host '本机模式；企微未连接；提醒在后台可见。'
Write-Host "登录口令文件：$taskAccessFile"
if (-not $NoBrowser) { Start-Process -FilePath $taskUrl }
