# deploy_bigqmt_windows.ps1 —— bigqmt 一键部署（Windows）
#
# 把 deploy-user-guide.md §3.1–3.4 自动化：
#   1) qmt-server 侧依赖安装（xtquant-big-convert[redis]，走 TUNA 镜像）
#   2) Redis 服务端：下载（api.github.com 资产通道）→ 校验 → 解压 → 启动 → PONG
#   3) QMT 侧文件部署（调用 scripts/deploy_bigqmt_server.py，绝不覆盖 local_config）
#   4) redis-py 3.5.3 复制进 QMT 内置 Python 3.6 目录
#   5) 内置解释器导入自检（pythonw 写日志回读）
#   6) 打印剩余的手工 UI 步骤（§3.5–3.6，无法脚本化）
#
# 用法（PowerShell）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\deploy_bigqmt_windows.ps1 `
#       -QmtPythonDir "C:\QMT_Simulator\python" -AccountId "88002471"
#
# 幂等：可重复执行。Redis 已在跑则跳过启动；redis-py 已存在则跳过；
#       local_config 已存在由部署脚本保留。
# 手工步骤与故障排查见仓库根 deploy-user-guide.md。

[CmdletBinding()]
param(
    # 完整版 QMT 客户端的 python 目录（内置 Python 3.6 沙箱）
    [string]$QmtPythonDir = "C:\QMT_Simulator\python",
    # QMT 绑定的资金账号（写入 local_config 模板，仅首次生成时生效）
    # 默认取本机 QMT_Simulator 的模拟账户；部署到其它客户端时务必覆盖
    [string]$AccountId = "88002471",
    [string]$RedisHost = "127.0.0.1",
    [int]$RedisPort = 6379,
    [int]$RedisDb = 5,
    # Redis 服务端安装目录（不存在则下载解压）
    [string]$RedisDir = "$env:USERPROFILE\bigqmt\redis",
    # xtquant import shim 导出目录（qmt-server 的 QMT_BRIDGE_BIGQMT_SHIM_DIR）
    [string]$ShimOutDir = "$env:USERPROFILE\bigqmt\shim",
    # pip 镜像（本机 PyPI CDN 被墙，默认 TUNA）
    [string]$PipIndex = "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple",
    # 仓库根（默认取脚本上级；UNC 路径下 pip -e 会失败，请传本地 junction 路径）
    [string]$RepoDir = "",
    [switch]$SkipServerInstall,   # 跳过步骤1（qmt-server 侧 pip）
    [switch]$SkipRedisInstall     # 跳过步骤2（假定 Redis 已就绪）
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Write-Step([string]$msg)  { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)    { Write-Host "    [OK] $msg" -ForegroundColor Green }
function Write-Warn2([string]$msg) { Write-Host "    [!!] $msg" -ForegroundColor Yellow }
function Fail([string]$msg)        { Write-Host "    [XX] $msg" -ForegroundColor Red; exit 1 }

# qmt_bridge 当前解析到哪个仓库（editable 可能指向旧/其它 checkout，见 §3.1 陷阱）
# 返回 "OK <模块路径>" / "MISMATCH <模块路径>" / "NOT_INSTALLED"
# 用 python 的 realpath 归一 junction/UNC，避免 C:\junction 与 \\host\... 比较误报
function Get-QmtBridgeResolution([string]$repo) {
    $esc = $repo.Replace("'", "\'")
    $code = @"
import os
try:
    import qmt_bridge
except ImportError:
    print("NOT_INSTALLED"); raise SystemExit
mod = os.path.normcase(os.path.realpath(qmt_bridge.__file__))
repo = os.path.normcase(os.path.realpath(r'$esc'))
print(("OK" if mod.startswith(repo + os.sep) else "MISMATCH") + " " + mod)
"@
    # PS5.1 向原生命令传多行参数会损坏代码，写临时 .py 执行
    $tmp = Join-Path $env:TEMP "bigqmt_editable_check.py"
    $code | Out-File -FilePath $tmp -Encoding ascii
    (& py $tmp 2>$null) -join "`n"
}

if (-not $RepoDir) { $RepoDir = Split-Path -Parent $PSScriptRoot }

# ---------------------------------------------------------------- 0. 预检
Write-Step "步骤 0/6 预检"
if (-not (Test-Path (Join-Path $QmtPythonDir "..\bin.x64\pythonw.exe"))) {
    Fail "未找到 $QmtPythonDir 同级的 bin.x64\pythonw.exe —— 请确认 -QmtPythonDir 指向完整版 QMT 的 python 目录"
}
Write-Ok "QMT 客户端: $(Split-Path -Parent $QmtPythonDir)  (内置 Py3.6)"
if ($RepoDir -like "\\*") {
    Write-Warn2 "仓库在 UNC 路径 ($RepoDir)，pip -e 可能失败；建议 -RepoDir 传本地路径（如 junction）"
}
Write-Ok "仓库: $RepoDir"

# editable 解析检查（防止旧 editable 指向其它 checkout 遮蔽本仓库）
$res = Get-QmtBridgeResolution $RepoDir
if ($res -like "MISMATCH*") {
    Write-Warn2 ("qmt_bridge 当前解析到别的仓库: " + ($res -replace '^(\w+) ', ''))
    Write-Warn2 "qmt-server 会用到旧代码（如 main 分支 2.9.11，无 server/trading/）—— 本脚本步骤 1 会把 editable 重指到本仓库"
}

# ---------------------------------------------------------------- 1. qmt-server 侧依赖
if ($SkipServerInstall) {
    Write-Step "步骤 1/6 qmt-server 侧依赖 —— 按参数跳过"
} else {
    Write-Step "步骤 1/6 qmt-server 侧依赖（xtquant-big-convert[redis]，镜像 $PipIndex）"
    Push-Location $RepoDir
    try {
        py -m pip install --user -e ".[bigqmt]" -i $PipIndex | Out-Null
        Write-Ok "已安装/校验 qmt-bridge[bigqmt]"
    } finally { Pop-Location }
    # 验证 editable 确已指向本仓库（junction/UNC 归一后比较）
    $res = Get-QmtBridgeResolution $RepoDir
    if ($res -like "OK*") {
        Write-Ok ("qmt_bridge 解析: " + ($res -replace '^\w+ ', ''))
    } elseif ($res -eq "NOT_INSTALLED") {
        Fail "pip install 后仍 import 不到 qmt_bridge —— 检查 py 版本/--user 目录"
    } else {
        Fail ("editable 仍指向别处: " + ($res -replace '^(\w+) ', '') + " —— 旧 editable 未被替换，请先 pip uninstall qmt-bridge 再重跑")
    }
}

# ---------------------------------------------------------------- 2. Redis 服务端
function Test-RedisUp {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $c.Connect($RedisHost, $RedisPort); $ok = $c.Connected; $c.Close()
        return $ok
    } catch { return $false }
}
if ($SkipRedisInstall) {
    Write-Step "步骤 2/6 Redis —— 按参数跳过"
    if (-not (Test-RedisUp)) { Fail "-SkipRedisInstall 但 $RedisHost`:$RedisPort 无监听" }
    Write-Ok "Redis 端口可连"
} elseif (Test-RedisUp) {
    Write-Step "步骤 2/6 Redis —— 已在运行，跳过安装/启动"
    Write-Ok "$RedisHost`:$RedisPort 已监听"
} else {
    Write-Step "步骤 2/6 Redis 服务端"
    if (-not (Test-Path (Join-Path $RedisDir "redis-server.exe"))) {
        Write-Host "    下载 tporadowski/Redis 5.0.14.1（约 12.6MB，本机通道 ~14KB/s，需 10-15 分钟）..."
        $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/tporadowski/redis/releases/latest" -TimeoutSec 60
        $asset = $rel.assets | Where-Object { $_.name -like "Redis-x64-*.zip" } | Select-Object -First 1
        if (-not $asset) { Fail "release 资产里未找到 Redis-x64-*.zip" }
        $tmpZip = Join-Path $env:TEMP $asset.name
        # api.github.com 资产通道（github.com 直连被墙的环境唯一可行路径）
        Invoke-WebRequest -Uri ("https://api.github.com/repos/tporadowski/redis/releases/assets/" + $asset.id) `
            -Headers @{ Accept = "application/octet-stream" } -OutFile $tmpZip -TimeoutSec 1800
        Expand-Archive -Path $tmpZip -DestinationPath $RedisDir -Force
        Remove-Item $tmpZip -Force
        Write-Ok "已解压到 $RedisDir"
    } else {
        Write-Ok "redis-server.exe 已存在，跳过下载"
    }
    Start-Process -FilePath (Join-Path $RedisDir "redis-server.exe") `
        -ArgumentList (Join-Path $RedisDir "redis.windows.conf"), "--dir", $RedisDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $RedisDir "server-stdout.log") `
        -RedirectStandardError  (Join-Path $RedisDir "server-stderr.log")
    $up = $false
    foreach ($i in 1..15) { Start-Sleep -Milliseconds 600; if (Test-RedisUp) { $up = $true; break } }
    if (-not $up) { Fail "redis-server 启动后 9 秒内未监听，看 $RedisDir\server-stderr.log" }
    Write-Ok "Redis 已启动（$RedisHost`:$RedisPort，bind 127.0.0.1）"
}
# PING/PONG（redis-cli 在则用之，给最硬的证据）
$redisCli = Join-Path $RedisDir "redis-cli.exe"
if (Test-Path $redisCli) {
    $pong = & $redisCli -h $RedisHost -p $RedisPort PING 2>$null
    if ($pong -eq "PONG") { Write-Ok "PING -> PONG" } else { Write-Warn2 "PING 未返回 PONG（$pong），继续但请人工确认" }
}

# ---------------------------------------------------------------- 3. QMT 侧文件部署
Write-Step "步骤 3/6 QMT 侧文件（deploy_bigqmt_server.py）"
$depArgs = @(
    (Join-Path $PSScriptRoot "deploy_bigqmt_server.py"),
    "--qmt-python-dir", $QmtPythonDir,
    "--account-id", $AccountId,
    "--redis-host", $RedisHost, "--redis-port", "$RedisPort", "--redis-db", "$RedisDb",
    "--shim-out", $ShimOutDir
)
& py @depArgs
if ($LASTEXITCODE -ne 0) { Fail "部署脚本退出码 $LASTEXITCODE" }
Write-Ok "QMT 侧文件 + local_config（已存在则保留）+ shim 导出完成"

# ---------------------------------------------------------------- 4. redis-py 3.5.3
Write-Step "步骤 4/6 redis-py 3.5.3 → QMT 内置 Python 3.6"
$qmtRedisPkg = Join-Path $QmtPythonDir "redis\__init__.py"
if (Test-Path $qmtRedisPkg) {
    Write-Ok "已存在，跳过（如需强制重装请先删除 $QmtPythonDir\redis）"
} else {
    $dl = Join-Path $env:TEMP "redis35"
    py -m pip download --no-deps "redis==3.5.3" -d $dl -i $PipIndex | Out-Null
    $whl = Join-Path $dl "redis-3.5.3-py2.py3-none-any.whl"
    if (-not (Test-Path $whl)) { Fail "未下载到 redis-3.5.3 wheel" }
    $x = Join-Path $dl "x"
    py -m zipfile -e $whl $x
    Copy-Item -Recurse (Join-Path $x "redis") (Join-Path $QmtPythonDir "redis")
    Remove-Item -Recurse -Force $dl
    Write-Ok "已复制 redis-py 3.5.3"
}

# ---------------------------------------------------------------- 5. 内置解释器自检
Write-Step "步骤 5/6 QMT 内置 Py3.6 导入自检"
$checkLog = "$env:USERPROFILE\bigqmt\py36_deploy_check.log"
New-Item -ItemType Directory -Force -Path (Split-Path $checkLog) | Out-Null
$checkPy = Join-Path $env:TEMP "bigqmt_py36_check.py"
@"
# -*- coding: utf-8 -*-
import sys
log = open(r"$checkLog", "w")
log.write("version: %s\n" % sys.version)
sys.path.insert(0, r"$QmtPythonDir")
try:
    import redis
    log.write("redis: %s\n" % redis.__version__)
except Exception:
    import traceback; traceback.print_exc(file=log)
try:
    import bigqmt_signal_trader
    log.write("bigqmt_signal_trader: OK\n")
except Exception:
    import traceback; traceback.print_exc(file=log)
log.close()
"@ | Out-File -FilePath $checkPy -Encoding ascii
# $QmtPythonDir = "<QMT>\python" → pythonw 在 "<QMT>\bin.x64\"
$pythonw = Join-Path (Split-Path -Parent $QmtPythonDir) "bin.x64\pythonw.exe"
& $pythonw $checkPy | Out-Null
# 等日志"存在且非空"（pythonw 无控制台，先建文件再写内容会有竞态）
$deadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline) {
    if ((Test-Path $checkLog) -and ((Get-Item $checkLog).Length -gt 0)) { break }
    Start-Sleep -Milliseconds 500
}
$logTxt = ""
if (Test-Path $checkLog) { $logTxt = [IO.File]::ReadAllText($checkLog) }
if (-not $logTxt.Trim()) { Fail "pythonw 自检 15 秒未产出日志内容（$checkLog）" }
Write-Host $logTxt.Trim()
if ($logTxt -match "Traceback") { Fail "内置 Py3.6 导入失败，见上面 traceback" }
Write-Ok "redis + bigqmt_signal_trader 均可在内置 Py3.6 导入"

# ---------------------------------------------------------------- 6. 剩余手工步骤
Write-Step "步骤 6/6 剩余手工步骤（QMT 客户端 UI，无法脚本化）"
Write-Host @"
    [A] 模型研究 → +新建策略 → Python策略 → 粘贴
        $QmtPythonDir\bigqmt_rpc_bootstrap.py 的全部内容 → 保存为 bigqmt_rpc_bootstrap
        （保存框可勾「如果文件存在,自动重命名」）→ 编译无报错
    [B] 模型交易 → 新建策略交易：策略类型=bigqmt_rpc_bootstrap，
        主图 000300，账号类型=股票，资金账号=$AccountId → 确定
        → 实例右键 运行模式=模拟信号 → 启动。
        ⚠ 不要勾「启动本地 python」
    [C] 验证：日志出现
        $QmtPythonDir\logs\bigqmt.log → [bigqmt_rpc] ... allow_order_methods=False
    [D] qmt-server 侧连通验证：
        `$env:QMT_BRIDGE_BIGQMT_SHIM_DIR='$ShimOutDir'; `$env:BIGQMT_ACCOUNT_ID='$AccountId'
        qmt-server --trader-backend bigqmt
    详见仓库根 deploy-user-guide.md（§3.5 起为手工步骤，§7 为故障排查）
"@ -ForegroundColor White

Write-Host "`n部署自动化部分全部完成 ✔  (下单门控 rpc_allow_order_methods=False 保持关闭)" -ForegroundColor Green
