<#
.SYNOPSIS
  Register LocalMeetingSum to start automatically when you log in, and open
  Windows Defender Firewall on port 788 for LAN access.

.DESCRIPTION
  Creates a Scheduled Task "LocalMeetingSum" that runs `python server.py`
  in the project directory after you sign in to Windows. The task auto-
  restarts on failure and survives reboots.

  Run as an Administrator from an elevated PowerShell prompt.

.EXAMPLE
  # Install (default)
  .\scripts\install_autostart.ps1

.EXAMPLE
  # Uninstall
  .\scripts\install_autostart.ps1 -Uninstall

.EXAMPLE
  # Custom python and project path
  .\scripts\install_autostart.ps1 `
      -PythonPath "C:\Users\Administrator\.conda\envs\lms\python.exe" `
      -ProjectDir "C:\Users\Administrator\LLMProjects\LocalMeetingSum"
#>
param(
    [string]$PythonPath  = "C:\Users\Administrator\.conda\envs\lms\python.exe",
    [string]$ProjectDir  = "C:\Users\Administrator\LLMProjects\LocalMeetingSum",
    [string]$TaskName    = "LocalMeetingSum",
    [int]   $Port        = 788,
    [switch]$Uninstall,
    [switch]$AtStartup       # use system startup trigger instead of at-logon
)

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Warning "请在管理员 PowerShell 里运行(右键 → '以管理员身份运行')。"
    exit 1
}

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已删除计划任务 $TaskName" -ForegroundColor Green
    } else {
        Write-Host "未找到计划任务 $TaskName" -ForegroundColor Yellow
    }
    Remove-NetFirewallRule -DisplayName "LocalMeetingSum :$Port" -ErrorAction SilentlyContinue
    Write-Host "已删除防火墙规则(若存在)" -ForegroundColor Green
    exit 0
}

# ---- Validate paths ----
if (-not (Test-Path $PythonPath))   { Write-Error "Python 不存在: $PythonPath";   exit 1 }
if (-not (Test-Path $ProjectDir))   { Write-Error "项目目录不存在: $ProjectDir"; exit 1 }
$serverScript = Join-Path $ProjectDir "server.py"
if (-not (Test-Path $serverScript)) { Write-Error "找不到 server.py: $serverScript"; exit 1 }

# ---- Build task ----
$action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "server.py" `
    -WorkingDirectory $ProjectDir

if ($AtStartup) {
    # Runs even if no user is logged in. Requires stored credentials.
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Highest
} else {
    # Runs the moment THIS user signs in. Simplest, no credential storage.
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
}

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 365) `
    -MultipleInstances IgnoreNew

# Register (overwrite if exists)
Register-ScheduledTask `
    -TaskName    $TaskName `
    -Action      $action `
    -Trigger     $trigger `
    -Settings    $settings `
    -Principal   $principal `
    -Description "LocalMeetingSum auto-start ($($PythonPath) server.py @ port $Port)" `
    -Force | Out-Null

Write-Host "已创建计划任务: $TaskName" -ForegroundColor Green
Write-Host "  Python:      $PythonPath"
Write-Host "  ProjectDir:  $ProjectDir"
Write-Host "  Trigger:     $(if ($AtStartup) {'开机(任何用户登录前)'} else {'当前用户登录后'})"

# ---- Firewall ----
$ruleName = "LocalMeetingSum :$Port"
Remove-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
New-NetFirewallRule `
    -DisplayName $ruleName `
    -Direction Inbound `
    -LocalPort $Port `
    -Protocol TCP `
    -Action Allow `
    -Profile Domain,Private | Out-Null
Write-Host "已开放防火墙端口 $Port (域/专用网络)" -ForegroundColor Green

Write-Host ""
Write-Host "立即启动一次: Start-ScheduledTask -TaskName $TaskName" -ForegroundColor Cyan
Write-Host "查看状态:     Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo" -ForegroundColor Cyan
Write-Host "本机访问:     http://localhost:$Port" -ForegroundColor Cyan
$lan = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias 'Ethernet*','Wi-Fi*' -ErrorAction SilentlyContinue | Where-Object {$_.IPAddress -notlike '169.254.*'} | Select-Object -First 1).IPAddress
if ($lan) { Write-Host "局域网访问:   http://${lan}:$Port" -ForegroundColor Cyan }
Write-Host "手机麦克风需 HTTPS — 见 README 里的 Tailscale 一节" -ForegroundColor Yellow
