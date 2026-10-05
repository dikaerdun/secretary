[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskStateFile = Join-Path $PSScriptRoot 'local-process.local.json'
if (-not (Test-Path -LiteralPath $taskStateFile -PathType Leaf)) {
    Write-Host '没有本机后台的进程记录；未停止任何其他进程。'
    return
}
try { $taskState = Get-Content -LiteralPath $taskStateFile -Raw | ConvertFrom-Json }
catch { throw '本机进程记录文件损坏；为避免误停其他进程，未发送停止请求。' }
$taskBasePython = ([string](& $taskPython -c 'import os, sys; print(os.path.realpath(sys._base_executable))')).Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $taskBasePython -PathType Leaf)) {
    throw '无法确定本机虚拟环境使用的 Python 解释器；未停止任何进程。'
}
if ($taskState.module -ne 'secretary.local' -or $taskState.app_root -ne $taskRoot -or
    $taskState.executable -ne $taskPython -or $taskState.process_executable -ne $taskBasePython -or
    $taskState.instance_id -notmatch '^[A-Za-z0-9]{1,64}$') {
    throw '本机进程记录的路径或身份不匹配；未停止任何进程。'
}
$taskExpectedStop = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "local-stop-$($taskState.instance_id).local"))
if ($taskState.stop_file -ne $taskExpectedStop) { throw '停止文件路径不匹配；未停止任何进程。' }

function Get-VerifiedLocalProcess {
    $taskProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$taskState.pid)" -ErrorAction SilentlyContinue
    if ($null -eq $taskProcess) { return $null }
    $taskCommand = [string]$taskProcess.CommandLine
    if ($taskProcess.ExecutablePath -ne $taskBasePython -or -not $taskCommand.Contains($taskPython) -or
        $taskCommand -notmatch '(?:^|\s)-m\s+secretary\.local(?:\s|$)' -or
        $taskCommand -notmatch ('(?:^|\s)--instance-id\s+' + [regex]::Escape($taskState.instance_id) + '(?:\s|$)') -or
        -not $taskCommand.Contains($taskStateFile)) { throw '该 PID 当前不属于记录中的本机后台；未停止任何其他进程。' }
    return $taskProcess
}

if ($null -eq (Get-VerifiedLocalProcess)) {
    Remove-Item -LiteralPath $taskStateFile
    Write-Host '本机后台已停止；已清理旧进程记录。'
    return
}
# The application observes this instance-specific request and closes its SQLite
# connections and background analysis before it exits.
@{ instance_id = $taskState.instance_id } | ConvertTo-Json -Compress | Set-Content -LiteralPath $taskExpectedStop -Encoding UTF8
$taskDeadline = [DateTime]::UtcNow.AddSeconds(15)
while ([DateTime]::UtcNow -lt $taskDeadline) {
    try { $taskWaitingProcess = Get-VerifiedLocalProcess }
    catch {
        # During orderly exit CIM can briefly return an incomplete process.
        # The application removes both instance files after closing SQLite.
        # Once that cleanup is observed, there is nothing left to terminate.
        if (-not (Test-Path -LiteralPath $taskStateFile) -and
            -not (Test-Path -LiteralPath $taskExpectedStop)) {
            Write-Host '本机后台已停止；资料和口令保留，下一次启动可继续使用。'
            return
        }
        # Wait for cleanup while identities are transient. The final fallback
        # still performs the strict check and never kills an unknown process.
        Start-Sleep -Milliseconds 250
        continue
    }
    if ($null -eq $taskWaitingProcess) {
        Write-Host '本机后台已停止；资料和口令保留，下一次启动可继续使用。'
        return
    }
    Start-Sleep -Milliseconds 250
}
# Recheck PID, interpreter path, module, nonce and state path immediately before
# the bounded fallback. No wildcard/process-name termination is permitted.
if ($null -ne (Get-VerifiedLocalProcess)) { Stop-Process -Id ([int]$taskState.pid) -ErrorAction Stop }
if (Test-Path -LiteralPath $taskExpectedStop) { Remove-Item -LiteralPath $taskExpectedStop }
if (Test-Path -LiteralPath $taskStateFile) { Remove-Item -LiteralPath $taskStateFile }
Write-Host '本机后台进程已停止；资料和口令保留。'
