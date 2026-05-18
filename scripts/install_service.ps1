<#
.SYNOPSIS
  把 LocalMeetingSum 注册成 Windows 真服务(用 NSSM)。

.DESCRIPTION
  和 install_autostart.ps1(计划任务)相比,这个走 NSSM 把 server.py 包成
  一个真正的 Windows 服务:
    - 开机即起,不需要任何人登录
    - 崩溃后自动重启
    - 在 services.msc 里可见,标准服务管理面板可控
    - 标准服务日志(轮转)

  默认以 LocalSystem 账号运行——可以正常用 NVIDIA GPU(现代驱动 session 0
  下 CUDA 没问题),不需要存密码。ModelScope 缓存路径通过环境变量指回
  当前用户的 ~/.cache,避免再下一遍 1.2GB 模型。

  需要 NSSM:  choco install nssm   或  https://nssm.cc/download
  需要管理员权限。

.EXAMPLE
  .\scripts\install_service.ps1

.EXAMPLE
  # 卸载
  .\scripts\install_service.ps1 -Uninstall

.EXAMPLE
  # 用你自己的账号跑(可以读到当前用户家目录里的缓存,不需要额外环境变量);
  # NSSM 会用 DPAPI 安全存储密码。
  .\scripts\install_service.ps1 -RunAsUser
#>
param(
    [string]$PythonPath   = "C:\Users\Administrator\.conda\envs\lms\python.exe",
    [string]$ProjectDir   = "C:\Users\Administrator\LLMProjects\LocalMeetingSum",
    [string]$ServiceName  = "LocalMeetingSum",
    [string]$DisplayName  = "LocalMeetingSum (本地会议转写)",
    [int]   $Port         = 788,
    [switch]$RunAsUser,
    [switch]$Uninstall
)

# ---- Admin check ----
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Warning "请在管理员 PowerShell 里运行(右键 → '以管理员身份运行')。"
    exit 1
}

# ---- NSSM check ----
$nssm = Get-Command nssm -ErrorAction SilentlyContinue
if (-not $nssm) {
    # Common install paths
    foreach ($p in @(
        "$env:ProgramData\chocolatey\bin\nssm.exe",
        "$env:ProgramFiles\nssm\win64\nssm.exe",
        "$env:ProgramFiles(x86)\nssm\win64\nssm.exe"
    )) {
        if (Test-Path $p) { $nssm = $p; break }
    }
}
if (-not $nssm) {
    Write-Error "未找到 nssm。安装方法二选一:`n  choco install nssm`n  从 https://nssm.cc/download 下载,把 win64\nssm.exe 加进 PATH"
    exit 1
}
$nssmCmd = if ($nssm -is [System.Management.Automation.CommandInfo]) { $nssm.Source } else { $nssm }

# ---- Uninstall path ----
if ($Uninstall) {
    & $nssmCmd stop $ServiceName 2>$null | Out-Null
    & $nssmCmd remove $ServiceName confirm 2>$null
    Remove-NetFirewallRule -DisplayName "LocalMeetingSum :$Port" -ErrorAction SilentlyContinue
    Write-Host "已卸载服务 $ServiceName 与防火墙规则" -ForegroundColor Green
    exit 0
}

# ---- Validate paths ----
if (-not (Test-Path $PythonPath))            { Write-Error "Python 不存在: $PythonPath"; exit 1 }
if (-not (Test-Path $ProjectDir))            { Write-Error "项目目录不存在: $ProjectDir"; exit 1 }
$serverPy = Join-Path $ProjectDir "server.py"
if (-not (Test-Path $serverPy))              { Write-Error "找不到 server.py: $serverPy"; exit 1 }

# ---- ModelScope / HuggingFace cache path of the current user ----
$userCacheRoot = Join-Path $env:USERPROFILE ".cache"
$msCache       = Join-Path $userCacheRoot "modelscope"
$hfHome        = Join-Path $userCacheRoot "huggingface"

# ---- Recreate the service idempotently ----
& $nssmCmd stop $ServiceName 2>$null | Out-Null
& $nssmCmd remove $ServiceName confirm 2>$null

& $nssmCmd install $ServiceName $PythonPath "server.py" | Out-Null
& $nssmCmd set $ServiceName DisplayName        $DisplayName | Out-Null
& $nssmCmd set $ServiceName Description        "Local meeting transcription + LLM summary (port $Port). Audio captured in browser, STT/LLM run on this GPU." | Out-Null
& $nssmCmd set $ServiceName AppDirectory       $ProjectDir | Out-Null
& $nssmCmd set $ServiceName Start              SERVICE_AUTO_START | Out-Null

# Logs
$logDir = Join-Path $ProjectDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
& $nssmCmd set $ServiceName AppStdout          (Join-Path $logDir "service.out.log") | Out-Null
& $nssmCmd set $ServiceName AppStderr          (Join-Path $logDir "service.err.log") | Out-Null
& $nssmCmd set $ServiceName AppRotateFiles     1 | Out-Null
& $nssmCmd set $ServiceName AppRotateOnline    1 | Out-Null
& $nssmCmd set $ServiceName AppRotateBytes     10485760 | Out-Null   # 10 MB
& $nssmCmd set $ServiceName AppStdoutCreationDisposition 4 | Out-Null  # append
& $nssmCmd set $ServiceName AppStderrCreationDisposition 4 | Out-Null

# Restart on crash
& $nssmCmd set $ServiceName AppExit Default Restart | Out-Null
& $nssmCmd set $ServiceName AppRestartDelay 5000 | Out-Null
& $nssmCmd set $ServiceName AppThrottle 10000 | Out-Null

# Environment: point caches at the current user so models aren't re-downloaded
# under C:\Windows\System32\config\systemprofile\.cache\
$envLines = @(
    "MODELSCOPE_CACHE=$msCache"
    "HF_HOME=$hfHome"
    "PYTHONUNBUFFERED=1"
    "PYTHONIOENCODING=utf-8"
) -join "`r`n"
& $nssmCmd set $ServiceName AppEnvironmentExtra $envLines | Out-Null

# Run-as identity
if ($RunAsUser) {
    Write-Host ""
    $u = "$env:USERDOMAIN\$env:USERNAME"
    $cred = Get-Credential -UserName $u -Message "输入 $u 的密码(NSSM 用 DPAPI 安全存储)"
    if (-not $cred) { Write-Error "已取消"; exit 1 }
    $plain = $cred.GetNetworkCredential().Password
    & $nssmCmd set $ServiceName ObjectName $u $plain | Out-Null
    Write-Host "服务将以 $u 运行" -ForegroundColor Green
} else {
    & $nssmCmd set $ServiceName ObjectName LocalSystem | Out-Null
    Write-Host "服务将以 LocalSystem 运行(GPU 可用,modelscope/hf 缓存指向 $userCacheRoot)" -ForegroundColor Green
}

# ---- Firewall ----
$ruleName = "LocalMeetingSum :$Port"
Remove-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -LocalPort $Port -Protocol TCP -Action Allow -Profile Domain,Private | Out-Null
Write-Host "已开放防火墙端口 $Port(域 / 专用网络)" -ForegroundColor Green

# ---- Start ----
& $nssmCmd start $ServiceName | Out-Null
Start-Sleep -Seconds 2
$status = & $nssmCmd status $ServiceName
Write-Host ""
Write-Host "服务: $ServiceName" -ForegroundColor Cyan
Write-Host "  状态:    $status"
Write-Host "  Python:  $PythonPath"
Write-Host "  目录:    $ProjectDir"
Write-Host "  日志:    $logDir\service.{out,err}.log"
Write-Host ""
Write-Host "常用命令:" -ForegroundColor Cyan
Write-Host "  nssm status  $ServiceName"
Write-Host "  nssm restart $ServiceName"
Write-Host "  nssm edit    $ServiceName       # GUI 配置面板"
Write-Host "  Get-Service  $ServiceName"
Write-Host ""
Write-Host "本机访问:     http://localhost:$Port"
$lan = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue `
        | Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' -and $_.AddressState -eq 'Preferred' } `
        | Select-Object -First 1).IPAddress
if ($lan) { Write-Host "局域网访问:   http://${lan}:$Port" }
Write-Host "手机访问麦克风:必须 HTTPS — 见 README 里的 Tailscale 或自签证书一节" -ForegroundColor Yellow
