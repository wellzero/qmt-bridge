<#
=====================================================================
 start_bigqmt_service.ps1 —— bigqmt 全链路一键拉起
=====================================================================

.SYNOPSIS
    确保 Redis、QMT 客户端在跑，然后启动 bigqmt 后端的 qmt-server。

.DESCRIPTION
    bigqmt 是一条三段链路（deploy-user-guide.md §5）：

        ① Redis（127.0.0.1:6379，RPC 消息中介）
        ② QMT 客户端（XtItClient.exe，RPC 服务端宿主 + FormulaServer 行情）
        ③ qmt-server --trader-backend bigqmt（本服务）

    本脚本 = 链路编排器：逐段检查，缺什么补什么，再调用
    serve_qmt_server.ps1 启动 ③（环境变量/预检逻辑在那边，不重复实现）。

    各段「缺了怎么办」：
      ① Redis 端口不通：有 redis-server.exe → 拉起（detached，参数与
         deploy_bigqmt_windows.ps1 完全一致）；没有 → 提示先跑
         just deploy-bigqmt-full（首次下载安装 Redis 属部署期的事）。
      ② XtItClient 没跑：找到 exe → 以 reboot 参数启动（本机验证过无人
         值守可拉起，自动登录未 100% 证实，启动后请瞄一眼登录态）；
         找不到 exe → 本段只能人工（行情/交易全依赖它）。
      ③ 直接交给 serve_qmt_server.ps1 -Backend bigqmt。

    无法脚本化的一段（只能人工确认）：QMT 里 bigqmt_rpc_bootstrap 的
    模型交易实例须「运行中」。休市时 QMT 引擎不驱动策略脚本属正常现象
    （bigqmt.log 为空、交易 RPC ping 超时），开盘后自动恢复。

.PARAMETER Port
    qmt-server 监听端口，默认 18888（透传给 serve_qmt_server.ps1）。

.PARAMETER AccountId
    bigqmt 资金账号，默认 88002471（须与 QMT 侧 local_config 一致）。

.PARAMETER QmtExe
    QMT 客户端 exe，默认自动探测 C:\QMT_Simulator\bin.x64\XtItClient.exe。

.PARAMETER RedisDir
    Redis 安装目录（redis-server.exe / redis.windows.conf 所在），
    默认 %USERPROFILE%\bigqmt\redis。

.PARAMETER Trading
    透传 --trading（启用交易模块）。

.PARAMETER CheckOnly
    链路健康检查：照常检查/拉起 ①Redis ②QMT 客户端，③ 只跑 serve 的
    预检（含版本一致性），不启动 qmt-server。

.PARAMETER LogLevel
    info（默认）/ debug / warning / error。

.PARAMETER Help
    打印快速用法后退出（完整文档：Get-Help 本脚本 -Full）。

.EXAMPLE
    just start-bigqmt-service
    # 检查 Redis + QMT → 前台启动 qmt-server（bigqmt 后端，18888）

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_bigqmt_service.ps1 -Port 18889 -Trading
    # 双后端并行场景：bigqmt 用 18889，另开窗口跑 mini 18888

.NOTES
    前台运行（Ctrl+C 停止 qmt-server；Redis/QMT 是 detached 的，不受影响）。
    使用手册：scripts/scripts-user-guide.md（含验证步骤与故障排查表）。
#>

[CmdletBinding()]
param(
    [int]$Port = 18888,
    [string]$AccountId = "88002471",
    [string]$QmtExe = "C:\QMT_Simulator\bin.x64\XtItClient.exe",
    [string]$RedisDir = "$env:USERPROFILE\bigqmt\redis",
    [string]$RedisHost = "127.0.0.1",
    [int]$RedisPort = 6379,
    [int]$RedisDb = 5,
    [switch]$Trading,
    [switch]$CheckOnly,
    [string]$LogLevel = "info",
    [switch]$Help
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$msg)  { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)    { Write-Host "    [OK] $msg" -ForegroundColor Green }
function Write-Warn2([string]$msg) { Write-Host "    [!!] $msg" -ForegroundColor Yellow }
function Fail([string]$msg)        { Write-Host "    [XX] $msg" -ForegroundColor Red; exit 1 }

if ($Help) {
    Write-Host @"
start_bigqmt_service.ps1 —— bigqmt 全链路一键拉起（Redis → QMT 客户端 → qmt-server）

用法:
    just start-bigqmt-service                                # 一键（18888）
    ... start_bigqmt_service.ps1 -Port 18889 -Trading        # 指定端口 + 启用交易
    ... start_bigqmt_service.ps1 -CheckOnly                  # 链路健康检查（不启动 qmt-server）
    ... start_bigqmt_service.ps1 -QmtExe "D:\国金QMT\bin.x64\XtItClient.exe"
                                                             # 其它 QMT 客户端

各段行为:
    ① Redis     端口不通且 redis-server.exe 在 → 自动拉起（detached）
                 exe 不在 → 提示先 just deploy-bigqmt-full
    ② QMT客户端 XtItClient 没跑 → 用 exe 以 reboot 参数启动（请瞄一眼登录态）
    ③ qmt-server 交给 serve_qmt_server.ps1 -Backend bigqmt（含包导入/
                 版本一致性/Redis/账号四项预检；QMT python 目录从
                 -QmtExe 自动推导）

人工确认项:
    QMT 里 bigqmt_rpc_bootstrap 实例须「运行中」（模型交易页）。
    休市时交易 RPC 不通属正常，开盘自动恢复；行情 FormulaServer 部分不受限。
"@ -ForegroundColor White
    exit 0
}

# ---------- 工具：TCP 探活 ----------
function Test-Tcp([string]$h, [int]$p) {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $c.Connect($h, $p); $ok = $c.Connected; $c.Close(); return $ok
    } catch { return $false }
}

# =================================================================
# ① Redis —— RPC 消息中介
# =================================================================
Write-Step "① Redis（$RedisHost`:$RedisPort）"
if (Test-Tcp $RedisHost $RedisPort) {
    Write-Ok "已在监听"
} else {
    $redisExe = Join-Path $RedisDir "redis-server.exe"
    if (-not (Test-Path $redisExe)) {
        Fail "Redis 未安装（$redisExe 不存在）—— 先跑 just deploy-bigqmt-full"
    }
    # 与 deploy_bigqmt_windows.ps1 步骤 2 完全一致的 detached 启动参数：
    # --dir 显式指定 → RDB 落 RedisDir；隐藏窗口 + 日志重定向。
    Start-Process -FilePath $redisExe `
        -ArgumentList (Join-Path $RedisDir "redis.windows.conf"), "--dir", $RedisDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $RedisDir "server-stdout.log") `
        -RedirectStandardError  (Join-Path $RedisDir "server-stderr.log")
    $up = $false
    foreach ($i in 1..15) { Start-Sleep -Milliseconds 600; if (Test-Tcp $RedisHost $RedisPort) { $up = $true; break } }
    if (-not $up) { Fail "redis-server 启动后 9 秒未监听，看 $RedisDir\server-stderr.log" }
    Write-Ok "已拉起（detached）"
}

# =================================================================
# ② QMT 客户端 —— RPC 服务端宿主 + FormulaServer 行情（58600）
# =================================================================
Write-Step "② QMT 客户端（XtItClient）"
$qmtProc = Get-Process XtItClient -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 }
if ($qmtProc) {
    Write-Ok "已在运行 (PID $($qmtProc[0].Id))（行情 FormulaServer 直连可用）"
} else {
    if (-not (Test-Path $QmtExe)) {
        # 行情/交易全依赖客户端：不在就终止，避免起一个"看似正常实则全超时"的服务。
        Fail "QMT 客户端未运行且未找到 exe（$QmtExe）—— 用 -QmtExe 指向实际安装路径，或先手工启动客户端"
    }
    Write-Warn2 "未运行 → 以 reboot 参数启动（自动登录未 100% 证实，起来后请确认登录态）"
    Start-Process -FilePath $QmtExe -ArgumentList "reboot"
    $up = $false
    foreach ($i in 1..30) { Start-Sleep -Milliseconds 1000; if (Get-Process XtItClient -ErrorAction SilentlyContinue) { $up = $true; break } }
    if ($up) {
        Write-Ok "QMT 客户端进程已出现（登录/行情就绪需几十秒，请稍候）"
    } else {
        Write-Warn2 "30 秒未见进程 —— 请手工启动客户端后重跑本脚本"
    }
}
# 策略实例（无法脚本化核查，只能提示）：休市时引擎不驱动策略属正常。
Write-Warn2 "请确认 QMT 模型交易页 bigqmt_rpc_bootstrap 实例「运行中」（休市时交易 RPC 不通属正常）"

# =================================================================
# ③ qmt-server（bigqmt 后端）—— 交给 serve_qmt_server.ps1
# =================================================================
Write-Step "③ qmt-server（bigqmt 后端，port=$Port）"
$serve = Join-Path $PSScriptRoot "serve_qmt_server.ps1"
if (-not (Test-Path $serve)) { Fail "找不到 $serve（两脚本须同目录）" }
# QMT python 目录由 -QmtExe 推导（<QMT>\bin.x64\XtItClient.exe →
# <QMT>\python），供 serve 侧做「客户端包 ↔ QMT 侧部署」一致性预检：
# QMT 侧是文件拷贝、不随 pip 走，editable 切换/升级后两边会漂移。
$qmtPythonDir = Join-Path (Split-Path -Parent (Split-Path -Parent $QmtExe)) "python"
# 开关参数不能带值跨界传递（-Trading:$false 经 powershell -File 会变成
# 字符串绑定报错）——按需追加裸 -Trading 即可。
$serveArgs = @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $serve,
    "-Backend", "bigqmt", "-Port", "$Port",
    "-AccountId", $AccountId,
    "-QmtPythonDir", $qmtPythonDir,
    "-RedisHost", $RedisHost, "-RedisPort", "$RedisPort", "-RedisDb", "$RedisDb",
    "-LogLevel", $LogLevel
)
if ($Trading) { $serveArgs += "-Trading" }
if ($CheckOnly) { $serveArgs += "-CheckOnly" }
& powershell @serveArgs
exit $LASTEXITCODE
