# ── Windows 开机自启动配置脚本 ────────────────────────────────────────────────
# 执行环境：Windows PowerShell（管理员）
# 用法：以管理员身份右键运行，或在管理员 PowerShell 中执行
# 功能：注册开机任务，Windows 启动时自动在 WSL2 中启动 IndexTTS2 服务

$TaskName = "IndexTTS2-AutoStart"
$WSLDistro = "Ubuntu-24.04"
$ServiceDir = "~/indextts2-service"

# 检查是否以管理员运行
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "请以管理员身份运行此脚本"
    exit 1
}

# 删除已有的同名任务（如有）
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# 创建启动任务
$Action = New-ScheduledTaskAction `
    -Execute "wsl.exe" `
    -Argument "-d $WSLDistro -e bash -c 'cd $ServiceDir && docker compose up -d >> ~/indextts2-startup.log 2>&1'"

$Trigger = New-ScheduledTaskTrigger -AtStartup

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Windows 启动时自动启动 IndexTTS2 TTS 服务" | Out-Null

Write-Host "✅ 开机自启动任务已注册: $TaskName"
Write-Host "   下次重启后将自动启动 IndexTTS2 服务"
Write-Host ""
Write-Host "管理命令："
Write-Host "  查看任务：Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "  手动运行：Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  删除任务：Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
