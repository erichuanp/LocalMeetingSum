<#
.SYNOPSIS
  Generate a self-signed TLS cert for LocalMeetingSum so phones can use
  getUserMedia (mic) over the LAN.

.DESCRIPTION
  Browsers refuse to grant mic permission over `http://` except on
  `localhost`. This script creates a self-signed cert + key under
  `certs/` and prints the env vars you need.

  After running, set in your `.env`:
      SSL_CERTFILE=certs/cert.pem
      SSL_KEYFILE=certs/key.pem
  Then restart server.py and access via:
      https://<machine-ip>:788

  Phones will show a security warning the first time — accept the
  exception once and the mic prompt works.

  For real Let's Encrypt certs over a `*.ts.net` domain, see the
  Tailscale section in README.md instead.

.EXAMPLE
  .\scripts\make_self_signed_cert.ps1
  .\scripts\make_self_signed_cert.ps1 -Hostnames "winhost.local","192.168.1.42"
#>
param(
    [string[]]$Hostnames,
    [string]$OutDir = "certs",
    [int]$ValidYears = 5
)

if (-not (Get-Command openssl -ErrorAction SilentlyContinue)) {
    Write-Error "需要 openssl。Windows 10/11 自带或装一个: choco install openssl"
    exit 1
}

if (-not $Hostnames -or $Hostnames.Count -eq 0) {
    $Hostnames = @("localhost", "127.0.0.1")
    $lan = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias 'Ethernet*','Wi-Fi*' -ErrorAction SilentlyContinue | Where-Object {$_.IPAddress -notlike '169.254.*'} | Select-Object -First 1).IPAddress
    if ($lan) { $Hostnames += $lan }
    $Hostnames += $env:COMPUTERNAME
}

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }

$cnf = Join-Path $OutDir "openssl.cnf"
$sanLines = @()
$dns = 0; $ip = 0
foreach ($h in $Hostnames) {
    if ($h -match '^\d+\.\d+\.\d+\.\d+$') { $ip++;  $sanLines += "IP.$ip = $h" }
    else                                  { $dns++; $sanLines += "DNS.$dns = $h" }
}
$sanBlock = ($sanLines -join "`n")

@"
[req]
distinguished_name = req
x509_extensions = v3_req
prompt = no

[req]
CN = LocalMeetingSum

[v3_req]
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt

[alt]
$sanBlock
"@ | Set-Content -Encoding ASCII $cnf

$days = $ValidYears * 365
$certPath = Join-Path $OutDir "cert.pem"
$keyPath  = Join-Path $OutDir "key.pem"

& openssl req -x509 -newkey rsa:2048 -nodes `
    -keyout $keyPath -out $certPath `
    -days $days -config $cnf -extensions v3_req 2>&1 | Out-Null

if (-not (Test-Path $certPath)) { Write-Error "证书生成失败"; exit 1 }

Write-Host "生成成功:" -ForegroundColor Green
Write-Host "  $certPath"
Write-Host "  $keyPath"
Write-Host ""
Write-Host "在 .env 里加:" -ForegroundColor Cyan
Write-Host "  SSL_CERTFILE=$($certPath -replace '\\','/')"
Write-Host "  SSL_KEYFILE=$($keyPath -replace '\\','/')"
Write-Host ""
Write-Host "访问(浏览器会提示证书不受信任,通过 '高级 → 继续访问' 例外一次即可):" -ForegroundColor Yellow
foreach ($h in $Hostnames) {
    if ($h -ne "localhost" -and $h -ne "127.0.0.1") {
        Write-Host "  https://${h}:788"
    }
}
