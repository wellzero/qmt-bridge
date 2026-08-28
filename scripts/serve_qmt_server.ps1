<#
=====================================================================
 serve_qmt_server.ps1 —— qmt-server 启动器（双后端可选）
=====================================================================

.SYNOPSIS
    启动 qmt-bridge API 服务，一键切换交易后端（mini / bigqmt）。

.DESCRIPTION
    mini 后端：XtQuantTrader 直连 miniQMT（原路径，行为与 just serve 一致）。
    bigqmt 后端：经 xtquant_big_convert 走 Redis RPC 到完整版 QMT 策略沙箱，
    自动设置账号/Redis 环境变量并做防呆预检（详见下方背景说明）。

.PARAMETER Backend
    mini（默认）或 bigqmt。

.PARAMETER Port
    监听端口，默认 18888。双后端并行时错开（如 mini 18888 + bigqmt 18889）。

.PARAMETER AccountId
    bigqmt 专用：QMT 资金账号，默认 88002471（须与 QMT 侧 local_config 一致）。

.PARAMETER QmtPythonDir
    bigqmt 专用：完整版 QMT 的 python 目录（服务端文件部署处），
    默认 C:\QMT_Simulator\python。用于客户端包 ↔ QMT 侧部署的一致性预检。

.PARAMETER RedisHost/RedisPort/RedisDb
    bigqmt 专用：Redis 三件套，默认 127.0.0.1:6379/db5。

.PARAMETER Trading
    透传 --trading（启用交易模块；bigqmt 交易需 QMT 端策略实例运行中）。

.PARAMETER CheckOnly
    只跑预检（包导入 / 版本一致性 / Redis / 账号），不启动 qmt-server。
    用作链路健康检查：just serve-backend bigqmt -CheckOnly。

.PARAMETER LogLevel
    info（默认）/ debug / warning / error。

.PARAMETER Help
    打印快速用法示例后退出（完整文档用 Get-Help 本脚本）。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\serve_qmt_server.ps1
    # mini 后端，端口 18888

.EXAMPLE
    ... serve_qmt_server.ps1 -Backend bigqmt -Port 18889
    # bigqmt 后端，端口 18889（本机 QMT_Simulator 默认账号）

.NOTES
    快捷方式：just serve-backend mini | just serve-backend bigqmt

【背景】qmt-bridge 支持两种交易后端（docs/big-qmt.md §3）：

    mini   策略 → qmt-server → XtQuantTrader 直连 miniQMT（原路径）
    bigqmt 策略 → qmt-server → BigQmtAdapter → Redis RPC → 完整版 QMT
                    策略沙箱内的 bigqmt_signal_trader 服务端

两条路径对行情 API 完全透明（/api/market/* 等不变），区别只在交易通道
与行情来源：bigqmt 模式下 xtdata 由 server/xtdata_source.py 解析到
xtquant-big-convert 包（from bigqmt_signal_trader.xtquant_compat import
xtdata，editable 安装 → 直连源码 src/ 本体），读取优先经 FormulaServer 直连
（127.0.0.1:58600，QMT 客户端开着即可用，无需策略运行），其余方法回落
Redis RPC（需 QMT 策略端运行，通常仅交易时段）。mini 模式维持原 xtquant
直连，行为与老 `just serve` 完全一致。

【本脚本做什么】把两种后端的启动差异（环境变量 + 参数）封装成一个开关：

    -Backend mini    # 默认。等价 qmt-server --trader-backend mini
    -Backend bigqmt  # 设账号/Redis 环境变量 + 预检（防呆），
                     # 再 qmt-server --trader-backend bigqmt --account-id ...

bigqmt 分支的四个预检（都为「跑到一半才发现环境错」的常见坑）：
  1. qmt-server 命令存在（editable 安装是否指向本仓库）；
  2. xtquant-big-convert 包可导入 + 版本号（没装先跑 just deploy-bigqmt-full）；
  3. 客户端包 ↔ QMT 侧部署文件一致性（QMT 侧是文件拷贝，不随 pip 走：
     editable 切换/升级后两边会漂移 —— 2026-08-28 本机实际发生过；
     不一致只告警不拦截，重跑部署步骤 3 即可同步并自动备份）；
  4. Redis 端口可连（bigqmt 交易 RPC 的消息中介，不通则交易全超时）。

【用法示例】
    # mini 后端（默认 18888，等价 just serve）：
    just serve-backend mini
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\serve_qmt_server.ps1

    # bigqmt 后端（本机 QMT_Simulator 默认账号 88002471）：
    just serve-backend bigqmt
    ... serve_qmt_server.ps1 -Backend bigqmt -Port 18889

    # 双后端并行（§6 步骤 3 字段比对）：开两个窗口，
    # 一个 -Backend mini -Port 18888，一个 -Backend bigqmt -Port 18889。

    # 部署到其它 QMT 客户端时覆盖账号：
    ... -Backend bigqmt -AccountId 12345678

【已知行为】
  - 前台运行，Ctrl+C 停止（与 just serve 一致）。
  - 交易模块默认不启用；加 -Trading 透传 --trading。
    bigqmt 交易 RPC 需要 QMT 端策略实例在跑（模型交易「运行中」且在
    交易时段），否则 connect 阶段 ping 超时 —— 服务仍会正常启动，
    行情（FormulaServer 直连部分）不受影响。
  - BIGQMT_ACCOUNT_ID 环境变量与 --account-id 双保险：不传账号时
    客户端会退到纸面账户（blue_chip_paper），请求写错队列。

【相关文件】
    scripts/scripts-user-guide.md       本脚本与 start_bigqmt_service.ps1 的使用手册
    scripts/deploy_bigqmt_windows.ps1   bigqmt 一键部署（装 xtquant-big-convert/Redis）
    deploy-user-guide.md §5.1           启动手册
#>

[CmdletBinding()]
param(
    # 交易后端：mini（默认，xtquant 直连 miniQMT）| bigqmt（Redis RPC）。
    [ValidateSet("mini", "bigqmt")]
    [string]$Backend = "mini",

    # 监听端口。默认 18888（与 serve-stop / deploy-user-guide §5.1 约定
    # 一致；双后端并行时错开，如 mini 18888 + bigqmt 18889）。
    [int]$Port = 18888,

    # bigqmt 专用：QMT 绑定的资金账号。客户端把它拼进 RPC 队列键
    # （bigqmt:rpc:queue:<账号>），必须与 QMT 侧 local_config 一致，
    # 否则请求写进 A 队列、服务端听 B 队列（表现为 ping 超时）。
    [string]$AccountId = "88002471",

    # bigqmt 专用：完整版 QMT 的 python 目录（deploy_bigqmt_server.py
    # 部署服务端文件处）。预检 3 用它做客户端包 ↔ QMT 侧部署的一致性比对
    # （比对受管文件：bigqmt_signal_trader 包 + 6 个顶层入口）。
    [string]$QmtPythonDir = "C:\QMT_Simulator\python",

    # bigqmt 专用：Redis 三件套（与 QMT 侧 local_config 同一套；db=5
    # 为上游约定的 bigqmt 专用库）。预检只探 TCP，不做 PING 强求。
    [string]$RedisHost = "127.0.0.1",
    [int]$RedisPort = 6379,
    [int]$RedisDb = 5,

    # 透传 --trading（启用交易模块；两后端通用）。
    [switch]$Trading,

    # 只跑预检不启动 qmt-server（链路健康检查 / 交接班确认用）。
    [switch]$CheckOnly,

    # 日志级别：info / debug / warning / error。
    [string]$LogLevel = "info",

    # 打印快速用法示例后退出（完整文档：Get-Help .\serve_qmt_server.ps1 -Full）。
    [switch]$Help
)

$ErrorActionPreference = "Stop"

# ---------- -Help：快速用法速查（Ctrl+C 停止；完整文档走 Get-Help） ----------
if ($Help) {
    Write-Host @"
serve_qmt_server.ps1 —— qmt-server 启动器（双后端：mini=xtquant 直连 | bigqmt=RPC）

用法:
    just serve-backend mini                                  # mini 后端（默认 18888）
    just serve-backend bigqmt                                # bigqmt 后端（推荐入口）
    ... serve_qmt_server.ps1 -Backend bigqmt -Port 18889     # 直接调用，指定端口
    ... serve_qmt_server.ps1 -Backend bigqmt -AccountId 12345678
                                                             # 部署到其它 QMT 客户端

常用参数:
    -Backend     mini | bigqmt            （默认 mini）
    -Port        监听端口，默认 18888      （双后端并行：mini 18888 + bigqmt 18889）
    -AccountId   bigqmt 资金账号，默认 88002471（须与 QMT 侧 local_config 一致）
    -QmtPythonDir  QMT python 目录（一致性预检用），默认 C:\QMT_Simulator\python
    -RedisHost/-RedisPort/-RedisDb   bigqmt Redis，默认 127.0.0.1:6379/db5
    -Trading     启用交易模块（透传 --trading）
    -LogLevel    info | debug | warning | error
    -CheckOnly   只跑预检不启动服务（链路健康检查）

提示:
    - bigqmt 行情（FormulaServer 直连部分）无需 QMT 策略运行，休市可用；
      交易 RPC 需 QMT 端模型交易实例「运行中」且在交易时段。
    - 完整文档: Get-Help .\serve_qmt_server.ps1 -Full
"@ -ForegroundColor White
    exit 0
}

# ---------- 输出助手（与 deploy_bigqmt_windows.ps1 同风格） ----------
function Write-Step([string]$msg)  { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)    { Write-Host "    [OK] $msg" -ForegroundColor Green }
function Write-Warn2([string]$msg) { Write-Host "    [!!] $msg" -ForegroundColor Yellow }
function Fail([string]$msg)        { Write-Host "    [XX] $msg" -ForegroundColor Red; exit 1 }

# ---------- 公共预检：qmt-server 命令可用 ----------
Write-Step "启动 qmt-server（backend=$Backend, port=$Port）"
if (-not (Get-Command qmt-server -ErrorAction SilentlyContinue)) {
    Fail "找不到 qmt-server 命令 —— 先 py -m pip install --user -e `".[full]`" 或跑 just deploy-bigqmt-full"
}

# 透传给 qmt-server 的公共参数（两后端一致）。
$commonArgs = @("--port", "$Port", "--log-level", $LogLevel)
if ($Trading) { $commonArgs += "--trading" }

if ($Backend -eq "bigqmt") {
    # ---------- bigqmt 专属预检 + 环境变量 ----------
    # bigqmt 行情通道 = xtquant-big-convert 包（xtdata_source 直接解析）
    $pkgOk = & py -c "import bigqmt_signal_trader" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Fail "xtquant-big-convert 未安装（import bigqmt_signal_trader 失败）—— 先跑 just deploy-bigqmt-full"
    }

    # 预检 2b/3：版本号留痕 + 客户端包 ↔ QMT 侧部署一致性。
    # QMT 侧是文件拷贝，不随 pip 走：editable 切换或升级后两边会漂移
    # （2026-08-28 本机实际发生过——editable 化让部署步骤 3 静默失效）。
    # 不一致只告警不拦截：行情走 FormulaServer 不受影响，重跑部署步骤 3
    # 即可同步（deploy_bigqmt_server.py 会先自动备份旧文件）。
    # 多行 python 必须写临时 .py（PS5.1 给 py -c 传多行会损坏）。
    $fpPy = Join-Path $env:TEMP "bigqmt_fp_check.py"
    @'
# -*- coding: utf-8 -*-
import hashlib, os, sys
import bigqmt_signal_trader as pkg

src = os.path.dirname(os.path.abspath(pkg.__file__))   # 客户端包实际落点
root = os.path.dirname(src)                            # 顶层入口所在根
dst_root = sys.argv[1] if len(sys.argv) > 1 else ""
TOP = [
    "BIGQMT_REDIS_DRYRUN.py",
    "bigqmt_signal_trader_strategy.py",
    "bigqmt_signal_trader_redis_rpc_runtime.py",
    "bigqmt_signal_trader_redis_dryrun.py",
    "bigqmt_signal_trader_dryrun.py",
    "bigqmt_signal_trader_diagnostic.py",
]

def snap(d):
    out = {}
    for r, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        for f in files:
            if f.endswith(".pyc"):
                continue
            p = os.path.join(r, f)
            out[os.path.relpath(p, d)] = hashlib.md5(open(p, "rb").read()).hexdigest()
    return out

def digest(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest() if os.path.isfile(p) else None

if dst_root and os.path.isdir(os.path.join(dst_root, "bigqmt_signal_trader")):
    a, b = snap(src), snap(os.path.join(dst_root, "bigqmt_signal_trader"))
    diff = [k for k in set(a) | set(b) if a.get(k) != b.get(k)]
    for name in TOP:
        if digest(os.path.join(root, name)) != digest(os.path.join(dst_root, name)):
            diff.append(name)
    print("MATCH" if not diff else "DIFF %d" % len(diff))
else:
    print("NODIR")
try:
    from importlib import metadata
    print("VER " + metadata.version("xtquant-big-convert"))
except Exception:
    print("VER ?")
'@ | Out-File -FilePath $fpPy -Encoding utf8
    $fpOut = & py $fpPy $QmtPythonDir 2>$null
    $fpFirst = "$($fpOut | Select-Object -First 1)"
    $ver = "?"
    foreach ($l in $fpOut) { if ("$l" -like "VER *") { $ver = "$l".Substring(4) } }
    Write-Ok "xtquant-big-convert $ver 包可用"
    if (-not $fpOut) {
        Write-Warn2 "一致性探测未能运行（py 失败）—— 跳过检查"
    } elseif ($fpFirst -eq "NODIR") {
        Write-Warn2 "未找到 QMT python 目录（$QmtPythonDir）—— 跳过一致性检查；部署过吗？（just deploy-bigqmt-full）"
    } elseif ($fpFirst -like "DIFF *") {
        $n = $fpFirst -replace "^DIFF ", ""
        Write-Warn2 "QMT 侧部署与客户端包不一致（$n 处文件）—— 重跑 just deploy-bigqmt-full -SkipServerInstall -SkipRedisInstall 同步（会自动备份）"
    } else {
        Write-Ok "QMT 侧部署与客户端包一致（$QmtPythonDir）"
    }

    function Test-Tcp([string]$h, [int]$p) {
        try {
            $c = New-Object Net.Sockets.TcpClient
            $c.Connect($h, $p); $ok = $c.Connected; $c.Close(); return $ok
        } catch { return $false }
    }
    if (-not (Test-Tcp $RedisHost $RedisPort)) {
        Fail "Redis $RedisHost`:$RedisPort 不可连 —— bigqmt 交易 RPC 会全部超时；先跑 just deploy-bigqmt-full 启动 Redis"
    }
    Write-Ok "Redis: $RedisHost`:$RedisPort (db=$RedisDb)"

    # 环境变量：RPC 客户端读 BIGQMT_* 三件套（行情通道由 xtdata_source
    # 在服务进程内按 --trader-backend 解析）。
    # --account-id 再传一道，防环境变量被外层覆盖的边角情况。
    $env:BIGQMT_ACCOUNT_ID = $AccountId
    $env:BIGQMT_REDIS_HOST = $RedisHost
    $env:BIGQMT_REDIS_PORT = "$RedisPort"
    $env:BIGQMT_REDIS_DB = "$RedisDb"
    Write-Ok "account: $AccountId（须与 QMT 侧 local_config 一致）"
    Write-Warn2 "交易 RPC 需 QMT 端策略实例运行中（交易时段）；行情 FormulaServer 直连部分不受此限"

    if ($CheckOnly) {
        Write-Host "`n预检通过，未启动服务（-CheckOnly）`n" -ForegroundColor Green
        exit 0
    }
    Write-Host "`n==> qmt-server --trader-backend bigqmt（前台，Ctrl+C 停止）`n" -ForegroundColor Cyan
    & qmt-server --trader-backend bigqmt --account-id $AccountId @commonArgs
} else {
    # ---------- mini：直连 miniQMT，零额外环境 ----------
    Write-Ok "mini 后端：XtQuantTrader 直连（需 miniQMT 客户端已登录）"
    if ($CheckOnly) {
        Write-Host "`n预检通过，未启动服务（-CheckOnly）`n" -ForegroundColor Green
        exit 0
    }
    Write-Host "`n==> qmt-server --trader-backend mini（前台，Ctrl+C 停止）`n" -ForegroundColor Cyan
    & qmt-server --trader-backend mini @commonArgs
}

exit $LASTEXITCODE
