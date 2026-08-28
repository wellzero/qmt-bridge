<#
=====================================================================
 deploy_bigqmt_windows.ps1 —— bigqmt 后端一键部署（Windows）
=====================================================================

【背景】
qmt-bridge 的 bigqmt 交易后端（docs/big-qmt.md §3）不在 qmt-server
进程里直接连 QMT，而是经 xtquant_big_convert 走 Redis RPC：

    策略 → qmt-server ─┬─ MiniQmtBackend ──► miniQMT（原路径，并行保留）
                       └─ BigQmtAdapter ──► Redis RPC ──► QMT 策略沙箱内的
                                                bigqmt_signal_trader 服务端

因此「部署」= 三件事：
  a) qmt-server 所用 Python 装 xtquant-big-convert 客户端；
  b) 一台可达的 Redis（本机最简单）；
  c) 把服务端包放进完整版 QMT 的内置 Python（3.6 沙箱）目录，
     并在 QMT 界面里挂成模型交易策略（RPC 服务由 QMT 回调驱动）。

【安全边界】下单默认关闭：生成的 QMT 侧 local_config 里
rpc_allow_order_methods=False，submit_order/cancel_order 会被服务端
拒绝 —— 双后端字段比对（§6 步骤 3）通过后才人工改 True 灰度（步骤 4）。

【本脚本做什么】deploy-user-guide.md §3.1–3.4 的自动化：
  步骤 1  qmt-server 侧依赖：pip install -e ".[bigqmt]"（TUNA 镜像）
          → 安装 xtquant-big-convert[redis]（客户端 RPC + 行情兼容层）
          → 验证 editable 确实指向本仓库（防旧 checkout 遮蔽）
  步骤 2  Redis 服务端：tporadowski/Redis 5.0.14.1 绿色版
          （api.github.com 资产通道下载）→ 解压 → detached 启动 → PONG
  步骤 3  QMT 侧文件：调用本仓库 scripts/deploy_bigqmt_server.py
          → bigqmt_signal_trader 包 + 6 个顶层入口（数量随上游版本，
            0.2.14 = 46 个文件）拷入 QMT python 目录；源取 import
            bigqmt_signal_trader 实际解析处（editable 安装常态）；
            覆盖前自动备份旧文件到 %USERPROFILE%\bigqmt\backup；
            生成 local_config（已存在绝不覆盖）
  步骤 4  redis-py 3.5.3 → 拷进 QMT 内置 Python 3.6 目录（纯 Python 依赖）
  步骤 5  用 QMT 自带 pythonw.exe 做导入自检（redis + bigqmt_signal_trader）
  步骤 6  打印剩余手工步骤清单（QMT 客户端内的 UI 操作，无法脚本化，
          原因见步骤 6 注释）

【前提条件】
  - 部署机与完整版 QMT 同机（或可达同一 Redis）；QMT 客户端已安装
    （本脚本不要求 QMT 正在运行；挂载策略时才需要）。
  - `py` 启动器可用（默认 Python ≥3.10，装 qmt-server 侧依赖）。
  - 网络：pip 走 -PipIndex 镜像；Redis 下载走 api.github.com
    （github.com 直连被墙的环境唯一可行通道，~14KB/s，首次约 10-15 分钟）。

【用法示例】
  # 本机默认（QMT_Simulator / 88002471 / 127.0.0.1:6379/db5）—— 无参一键：
  just deploy-bigqmt-full
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\deploy_bigqmt_windows.ps1

  # 部署到其它 QMT 客户端（改两个参数即可）：
  ... -QmtPythonDir "D:\国金证券QMT交易端\python" -AccountId 12345678

  # 只重跑 QMT 侧文件 + 自检（依赖与 Redis 已就绪时）：
  ... -SkipServerInstall -SkipRedisInstall

  # 仓库在 UNC 共享上时，pip -e 请传本地 junction 路径：
  ... -RepoDir "C:\Users\Docker\Desktop\Shared\qmt-bridge-big-qmt"

【幂等性】可重复执行：
  - Redis 已监听 → 跳过下载/启动；redis-server.exe 已存在 → 跳过下载
  - QMT python 目录已有 redis-py → 跳过复制
  - local_config 已存在 → 由部署脚本保留（含账号/Redis 凭据，绝不覆盖）
  - 覆盖前自动备份旧的受管文件（包 + 6 入口，不含 local_config）到
    %USERPROFILE%\bigqmt\backup\bigqmt_server_pre_<时间戳>.zip

【退出行为】任何步骤失败：红色 [XX] 提示后 exit 1；全部通过 exit 0。

【已知环境坑（都已在代码里处理，改代码前先看这里）】
  - 本文件必须 UTF-8 带 BOM：PowerShell 5.1 对无 BOM 文件按 ANSI/GBK
    解析，中文注释会撕裂字符串字面量导致整脚本解析失败。
  - PS 5.1 向原生命令（py -c）传多行参数会损坏代码 → 一律写临时 .py。
  - UNC 路径作 pip -e 的源不可靠 → 预检告警，建议 -RepoDir 传本地路径。
  - $PSScriptRoot 可能被 junction 解析成 UNC 目标 → 同上。
  - 输出中文在 GBK 控制台（如经 bash 管道）会显示乱码，属显示问题非数据问题。

【相关文件】
  scripts/deploy_bigqmt_server.py   实际执行 QMT 侧文件部署（步骤 3 调用）
  deploy-user-guide.md              完整手册：手工步骤 §3.5–3.6、故障排查 §7
  docs/big-qmt.md                   设计文档（§3.5 部署、§6 灰度步骤）
#>

[CmdletBinding()]
param(
    # 完整版 QMT 客户端的 python 目录（策略沙箱，内置 Python 3.6）。
    # 判定方法：其同级 bin.x64\ 下应有 pythonw.exe / python36.dll。
    # 例: C:\QMT_Simulator\python、D:\国金证券QMT交易端\python
    [string]$QmtPythonDir = "C:\QMT_Simulator\python",

    # QMT 绑定的资金账号：写入 QMT 侧 local_config 的 BIGQMT_ACCOUNT_ID，
    # 同时是 Redis RPC 队列键的一部分（bigqmt:rpc:req:<AccountId>）。
    # 仅在 local_config 首次生成时生效（已存在则保留原值）。
    # 默认取本机 QMT_Simulator 的模拟账户；部署到其它客户端务必覆盖！
    # 注意：qmt-server 侧环境变量 BIGQMT_ACCOUNT_ID 必须与这里一致，
    # 否则客户端把请求写进 A 账号队列、服务端却监听 B 账号队列（ping 超时）。
    [string]$AccountId = "88002471",

    # Redis 连接三件套：QMT 侧 local_config 与 qmt-server 侧 env 共用同一套。
    # db=5 是上游约定（bigqmt 专用库，避免与其它用途混用 key 空间）。
    [string]$RedisHost = "127.0.0.1",
    [int]$RedisPort = 6379,
    [int]$RedisDb = 5,

    # Redis 服务端安装目录：不存在 redis-server.exe 时自动下载解压到这里。
    # RDB/AOF 持久化文件也落此目录（启动参数 --dir 显式指定）。
    [string]$RedisDir = "$env:USERPROFILE\bigqmt\redis",

    # pip 镜像。默认 TUNA：本机 PyPI CDN（files.pythonhosted.org）被墙。
    # 换环境如可直连 PyPI，传 -PipIndex https://pypi.org/simple。
    [string]$PipIndex = "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple",

    # qmt-bridge 仓库根（含 pyproject.toml）。步骤 1 的 `pip install -e .`
    # 以它为源 —— editable 安装不复制代码，import qmt_bridge 永远解析回
    # 这个目录，所以「指向哪个 checkout」决定了 qmt-server 跑的是哪套代码。
    # 默认 = 本脚本所在目录的上级；经 junction 调用时可能解析成 UNC，
    # pip -e 对 UNC 不可靠 → 预检会告警，此时请显式传本地路径。
    [string]$RepoDir = "",

    # 跳过步骤 1（不 pip、不校验 editable）。适用于依赖已装好、
    # 只想重刷 QMT 侧文件/自检的场景（注意跳过后不再拦截旧 editable 遮蔽）。
    [switch]$SkipServerInstall,

    # 跳过步骤 2（不下载/不启动 Redis），但仍要求端口可连（防呆）。
    [switch]$SkipRedisInstall
)

# Stop：任何 cmdlet 异常即终止（配合下方 Fail 统一出口）。
# TLS12：api.github.com / TUNA 均要求；PS5.1 默认不含。
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ---------- 输出助手：统一前缀 + 颜色，便于在长输出里扫读 ----------
function Write-Step([string]$msg)  { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)    { Write-Host "    [OK] $msg" -ForegroundColor Green }
function Write-Warn2([string]$msg) { Write-Host "    [!!] $msg" -ForegroundColor Yellow }
# 失败即终止（exit 1）；调用方拿不到对象返回，报错信息里都带下一步动作。
function Fail([string]$msg)        { Write-Host "    [XX] $msg" -ForegroundColor Red; exit 1 }

<#
Get-QmtBridgeResolution —— 探测 `import qmt_bridge` 实际解析到哪个仓库。

为什么需要：pip 的 editable 安装（-e）不复制代码，只写一个指向源仓库的
链接。本机若曾对其它 checkout（如 main 分支 \\host.lan\Data\qmt-bridge）
做过 editable 安装，`import qmt_bridge` 会拿到旧代码（缺 server/trading/
bigqmt 后端），qmt-server --trader-backend bigqmt 直接报无此参数 ——
而且错误发生在部署之后的运行期，很难追。故预检告警 + 装后强制验证。

返回协议（单行字符串）：
  "OK <模块绝对路径>"        解析在本仓库内（含 junction 穿透后的 UNC）
  "MISMATCH <模块绝对路径>"  解析在别的仓库 —— 就是上述遮蔽场景
  "NOT_INSTALLED"            还没装过 qmt_bridge（首装属正常）

实现要点：
  - 路径归一用 python 的 realpath+normcase：C:\junction\... 与其 UNC 目标
    \\host\... 归一后相同，避免「明明同一个仓库却报 MISMATCH」的误报。
  - 代码写临时 .py 再 `py <file>` 执行：PS5.1 给原生命令传多行参数
    会在换行处损坏代码（-c 不可靠）。
#>
function Get-QmtBridgeResolution([string]$repo) {
    $esc = $repo.Replace("'", "\'")   # 防路径里带单引号截断 r'...' 字面量
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
    $tmp = Join-Path $env:TEMP "bigqmt_editable_check.py"
    # UTF-8（PS5.1 带 BOM，Python3 接受）而非 ascii：仓库路径可能含中文，
    # ascii 会把非 ASCII 字符打成 ?，路径错 → 误报 MISMATCH。
    $code | Out-File -FilePath $tmp -Encoding utf8
    (& py $tmp 2>$null) -join "`n"
}

# 仓库根默认取「本脚本所在目录的上级」。注意经 junction 调用时
# $PSScriptRoot 可能已是 UNC 目标路径 —— 下方预检会针对 UNC 告警。
if (-not $RepoDir) { $RepoDir = Split-Path -Parent $PSScriptRoot }

# =================================================================
# 步骤 0/6 预检 —— 花几秒钟，把「跑到一半才发现参数错」扼杀在开头
# =================================================================
Write-Step "步骤 0/6 预检"
# pythonw.exe 是完整版 QMT 的特征文件（内置解释器的无窗入口）。
# miniQMT / 只有部分目录的安装没有它 —— 指错目录在这里立刻报，
# 而不是等步骤 5 自检莫名失败。
if (-not (Test-Path (Join-Path $QmtPythonDir "..\bin.x64\pythonw.exe"))) {
    Fail "未找到 $QmtPythonDir 同级的 bin.x64\pythonw.exe —— 请确认 -QmtPythonDir 指向完整版 QMT 的 python 目录"
}
Write-Ok "QMT 客户端: $(Split-Path -Parent $QmtPythonDir)  (内置 Py3.6)"
# UNC 告警：pip -e 的源在 UNC 上时 egg-link/扫描不可靠；解决法见参数说明。
if ($RepoDir -like "\\*") {
    Write-Warn2 "仓库在 UNC 路径 ($RepoDir)，pip -e 可能失败；建议 -RepoDir 传本地路径（如 junction）"
}
Write-Ok "仓库: $RepoDir"
# 把关键目录在开头亮出来。
Write-Ok "Redis 目录: $RedisDir"

# editable 遮蔽检查（详见 Get-QmtBridgeResolution 注释）。
# 这里只告警不终止：步骤 1 的 pip install 本来就会重指 editable；
# 真正强制校验在步骤 1 之后。配合 -SkipServerInstall 时此告警就是唯一防线。
$res = Get-QmtBridgeResolution $RepoDir
if ($res -like "MISMATCH*") {
    Write-Warn2 ("qmt_bridge 当前解析到别的仓库: " + ($res -replace '^(\w+) ', ''))
    Write-Warn2 "qmt-server 会用到旧代码（如 main 分支 2.9.11，无 server/trading/）—— 本脚本步骤 1 会把 editable 重指到本仓库"
}

# =================================================================
# 步骤 1/6 qmt-server 侧依赖 —— xtquant-big-convert[redis]（客户端）
# =================================================================
# 说明：
#   pyproject.toml 的 bigqmt extra = xtquant-big-convert[redis]>=0.2.9。
#   它提供 RPC 客户端 + 行情兼容层（bigqmt_signal_trader.xtquant_compat）：
#   BigQmtAdapter（交易）与 server/xtdata_source.py（行情）都直接 import 它，
#   不经任何 sys.path 间接层 —— editable 安装即直连源码 src/ 本体。
#   -e（editable）+ 本仓库：import qmt_bridge 跟随当前 checkout，
#   分支切换即刻生效，无需反复 pip install。
#   --user：装进用户站点包，无需管理员权限（本机无提权环境）。
if ($SkipServerInstall) {
    Write-Step "步骤 1/6 qmt-server 侧依赖 —— 按参数跳过"
} else {
    Write-Step "步骤 1/6 qmt-server 侧依赖（xtquant-big-convert[redis]，镜像 $PipIndex）"
    Push-Location $RepoDir          # pip -e ".[bigqmt]" 需在仓库根执行（找 pyproject.toml）
    try {
        py -m pip install --user -e ".[bigqmt]" -i $PipIndex | Out-Null
        Write-Ok "已安装/校验 qmt-bridge[bigqmt]"
    } finally { Pop-Location }      # finally：pip 失败也要把 cwd 还回去
    # 强制验证：装完必须解析到本仓库。三种结果三种处理（见函数注释）。
    # MISMATCH 场景 = 存在更早的 editable 指向别的 checkout 且未被本次替换
    # （比如旧安装用了不同路径形式），处理法：pip uninstall qmt-bridge 后重跑。
    $res = Get-QmtBridgeResolution $RepoDir
    if ($res -like "OK*") {
        Write-Ok ("qmt_bridge 解析: " + ($res -replace '^\w+ ', ''))
    } elseif ($res -eq "NOT_INSTALLED") {
        Fail "pip install 后仍 import 不到 qmt_bridge —— 检查 py 版本/--user 目录"
    } else {
        Fail ("editable 仍指向别处: " + ($res -replace '^(\w+) ', '') + " —— 旧 editable 未被替换，请先 pip uninstall qmt-bridge 再重跑")
    }
}

# =================================================================
# 步骤 2/6 Redis 服务端 —— RPC 的消息中介
# =================================================================
# 选型 tporadowski/Redis 5.0.14.1（绿色 zip，免安装免提权）的原因：
#   - conda-forge 没有 win64 原生 redis-server；
#   - 官方 MSI / tporadowski MSI 安装需要管理员权限（本机无提权）。
# 下载通道走 api.github.com 的 release 资产端点：github.com 直连被墙，
# 而 api.github.com 可达（慢，~14KB/s，12.6MB ≈ 10-15 分钟，仅首次）。
# 协议兼容：Redis 5.0 只讲 RESP2 —— 客户端兼容层默认 protocol=2，
# 无需任何额外配置（redis-py 8.x 才默认 RESP3，故别手贱升级客户端协议）。
# 用 TCP 连接（而非 redis-cli）做存活判定：判定时 redis-cli 可能还没解压。

# TCP 探活：能建立连接即认为服务在（Redis 接受连接 = 服务进程活着）。
function Test-RedisUp {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $c.Connect($RedisHost, $RedisPort); $ok = $c.Connected; $c.Close()
        return $ok
    } catch { return $false }
}
if ($SkipRedisInstall) {
    Write-Step "步骤 2/6 Redis —— 按参数跳过"
    # 防呆：既然声明 Redis 就绪，那就必须真的在（否则后面全是静默超时）。
    if (-not (Test-RedisUp)) { Fail "-SkipRedisInstall 但 $RedisHost`:$RedisPort 无监听" }
    Write-Ok "Redis 端口可连"
} elseif (Test-RedisUp) {
    # 幂等：已在跑（可能是上次部署启动的，也可能是共享的既有实例）就完全跳过。
    Write-Step "步骤 2/6 Redis —— 已在运行，跳过安装/启动"
    Write-Ok "$RedisHost`:$RedisPort 已监听"
} else {
    Write-Step "步骤 2/6 Redis 服务端"
    if (-not (Test-Path (Join-Path $RedisDir "redis-server.exe"))) {
        Write-Host "    下载 tporadowski/Redis 5.0.14.1（约 12.6MB，本机通道 ~14KB/s，需 10-15 分钟）..."
        # releases/latest 拿资产元数据（名称+id），再按 id 走八进制流下载。
        $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/tporadowski/redis/releases/latest" -TimeoutSec 60
        $asset = $rel.assets | Where-Object { $_.name -like "Redis-x64-*.zip" } | Select-Object -First 1
        if (-not $asset) { Fail "release 资产里未找到 Redis-x64-*.zip" }
        $tmpZip = Join-Path $env:TEMP $asset.name
        # 资产端点 + Accept: octet-stream = 直接流式返回文件本体（绕开被墙的
        # objects.githubusercontent.com 跳转）。TimeoutSec 1800 容纳慢通道。
        Invoke-WebRequest -Uri ("https://api.github.com/repos/tporadowski/redis/releases/assets/" + $asset.id) `
            -Headers @{ Accept = "application/octet-stream" } -OutFile $tmpZip -TimeoutSec 1800
        Expand-Archive -Path $tmpZip -DestinationPath $RedisDir -Force
        Remove-Item $tmpZip -Force
        Write-Ok "已解压到 $RedisDir"
    } else {
        Write-Ok "redis-server.exe 已存在，跳过下载"
    }
    # detached 启动（Start-Process 不随本脚本/会话退出而死）：
    #   --dir 显式指定工作目录 → RDB 快照落在 $RedisDir 而不是随机 cwd；
    #   隐藏窗口 + 重定向日志 → 开机后人工/计划任务重复执行本脚本即可拉起。
    # 注意：脚本不会把 Redis 注册成 Windows 服务 —— 重启机器后需重跑本脚本
    # （或自建计划任务），详见 deploy-user-guide.md §6。
    Start-Process -FilePath (Join-Path $RedisDir "redis-server.exe") `
        -ArgumentList (Join-Path $RedisDir "redis.windows.conf"), "--dir", $RedisDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $RedisDir "server-stdout.log") `
        -RedirectStandardError  (Join-Path $RedisDir "server-stderr.log")
    # 观测就绪而非固定 sleep：最多 15×0.6s=9s，端口开了立刻往下走。
    $up = $false
    foreach ($i in 1..15) { Start-Sleep -Milliseconds 600; if (Test-RedisUp) { $up = $true; break } }
    if (-not $up) { Fail "redis-server 启动后 9 秒内未监听，看 $RedisDir\server-stderr.log" }
    Write-Ok "Redis 已启动（$RedisHost`:$RedisPort，bind 127.0.0.1）"
}
# PING/PONG：TCP 通只证明进程在，PONG 才证明协议栈真的应答。
# redis-cli 已解压时才做（远端 Redis 场景本机可能没有 cli，跳过不强求）。
$redisCli = Join-Path $RedisDir "redis-cli.exe"
if (Test-Path $redisCli) {
    $pong = & $redisCli -h $RedisHost -p $RedisPort PING 2>$null
    if ($pong -eq "PONG") { Write-Ok "PING -> PONG" } else { Write-Warn2 "PING 未返回 PONG（$pong），继续但请人工确认" }
}

# =================================================================
# 步骤 3/6 QMT 侧文件部署 —— 真正「把服务端放进 QMT」的一步
# =================================================================
# 实际干活的是本仓库 scripts/deploy_bigqmt_server.py（本脚本只是编排者），
# 它做四件事，全部幂等：
#   1. 解析源目录：优先 import bigqmt_signal_trader 的实际落点 —— editable
#      安装（本机常态）下 pip 元数据的 dist.files 只有 finder 记录，
#      旧版“从 wheel 提取”会直接报“找不到 bigqmt_signal_trader 包”；
#      wheel 常规安装退回元数据定位，再不行 --source-dir 显式指定。
#      输出里带版本号（xtquant-big-convert 0.2.14 …），部署留痕可查。
#   2. 覆盖前把已存在的受管文件（包 + 6 入口，不含 local_config ——
#      凭据不进备份 zip）打包到 %USERPROFILE%\bigqmt\backup。
#   3. 拷入 bigqmt_signal_trader\ 包（RPC 服务端本体，纯 stdlib + 惰性
#      import）+ 6 个顶层入口（BIGQMT_REDIS_DRYRUN.py、bigqmt_signal_
#      trader_redis_rpc_runtime.py 运行时等）；拷完清掉目标包内的
#      __pycache__，并对上游已删除而目标残留的文件打 [遗留] 告警。
#   4. 生成 bigqmt_signal_trader_local_config.py（账号 + Redis + 下单门控
#      rpc_allow_order_methods=False）。含敏感信息 —— 已存在则【绝不覆盖】，
#      这是设计约束（§3.5），改账号/Redis 请手工编辑该文件。
# 注意：QMT 自带的示例策略文件（网格策略.py 等）完全不会被触碰。
Write-Step "步骤 3/6 QMT 侧文件（deploy_bigqmt_server.py）"
$depArgs = @(
    (Join-Path $PSScriptRoot "deploy_bigqmt_server.py"),
    "--qmt-python-dir", $QmtPythonDir,
    "--account-id", $AccountId,
    "--redis-host", $RedisHost, "--redis-port", "$RedisPort", "--redis-db", "$RedisDb"
)
& py @depArgs
if ($LASTEXITCODE -ne 0) { Fail "部署脚本退出码 $LASTEXITCODE" }
Write-Ok "QMT 侧文件 + local_config（已存在则保留）部署完成"

# =================================================================
# 步骤 4/6 redis-py 3.5.3 → QMT 内置 Python 3.6
# =================================================================
# 为什么是 3.5.3：QMT 内置解释器是 Python 3.6.8，redis-py 4.x 起要求
# ≥3.7；3.5.3 是最后支持 3.6 的稳定线，且纯 Python（无 C 扩展），
# 解包即用 —— 这正好绕开 QMT 沙箱装第三方包的老 OpenSSL SSL 坑（§3.5）。
# 为什么不用 pip 装：pip 只会装进【当前】解释器的站点包；目标是 QMT 的
# 3.6 沙箱，所以走「下载 wheel → 解包 → 拷包目录」的物理路线。
# 为什么拷进 python 目录：QMT 沙箱把该目录放在 sys.path 上（策略就住这），
# bigqmt_signal_trader 惰性 `import redis` 时从这里命中。
Write-Step "步骤 4/6 redis-py 3.5.3 → QMT 内置 Python 3.6"
$qmtRedisPkg = Join-Path $QmtPythonDir "redis\__init__.py"
if (Test-Path $qmtRedisPkg) {
    # 幂等跳过；版本没得挑（3.5.3 唯一），不存在装错版本的问题。
    Write-Ok "已存在，跳过（如需强制重装请先删除 $QmtPythonDir\redis）"
} else {
    $dl = Join-Path $env:TEMP "redis35"
    # --no-deps：3.5.3 无依赖；顺带避免解析器节外生枝。
    py -m pip download --no-deps "redis==3.5.3" -d $dl -i $PipIndex | Out-Null
    $whl = Join-Path $dl "redis-3.5.3-py2.py3-none-any.whl"
    if (-not (Test-Path $whl)) { Fail "未下载到 redis-3.5.3 wheel" }
    $x = Join-Path $dl "x"
    py -m zipfile -e $whl $x          # wheel 就是 zip：解出 redis\ 包目录
    Copy-Item -Recurse (Join-Path $x "redis") (Join-Path $QmtPythonDir "redis")
    Remove-Item -Recurse -Force $dl    # 清理临时下载
    Write-Ok "已复制 redis-py 3.5.3"
}

# =================================================================
# 步骤 5/6 QMT 内置 Py3.6 导入自检 —— 在【真解释器】里证明能跑
# =================================================================
# 上一步拷的依赖、步骤 3 部署的包，最终要在 QMT 内置 3.6.8 里被 import。
# 用 QMT 自带的 pythonw.exe（bin.x64\，与客户端同进程族）执行探测脚本：
#   - pythonw 无控制台 → 脚本把结果写到日志文件，PS 侧回读；
#   - 等「文件存在且非空」而非等进程退出：文件先创建后写入有竞态，
#     空文件读到 $null 会对后续 .Trim() 抛 InvokeMethodOnNull；
#   - 出现 Traceback 即 Fail（最常见：redis 目录拷错位置、
#     或 bigqmt_signal_trader 拷漏文件）。
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
"@ | Out-File -FilePath $checkPy -Encoding utf8   # 同上：$QmtPythonDir 可能含中文（如 D:\国金证券QMT交易端）
# 先删旧日志再启动：轮询判据是「文件存在且非空」，上次运行留下的旧日志
# 会让轮询立刻命中旧内容（新 pythonw 还没来得及截断文件），一次失败的
# 自检可能被上一轮的旧 OK 掩盖 —— 删掉就只剩「本轮结果」一种可能。
Remove-Item $checkLog -Force -ErrorAction SilentlyContinue
# $QmtPythonDir = "<QMT>\python" → pythonw 在 "<QMT>\bin.x64\"
$pythonw = Join-Path (Split-Path -Parent $QmtPythonDir) "bin.x64\pythonw.exe"
& $pythonw $checkPy | Out-Null
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

# =================================================================
# 步骤 6/6 剩余手工步骤 —— 为什么自动化到此为止
# =================================================================
# QMT 的策略列表来自内部索引（编辑器保存时写入），不扫描 python 目录；
# 已注册策略文件被 ACL 锁定（Users 只读）且加密存储。因此把 bootstrap
# 「注册成策略 + 挂成模型交易实例」只能走客户端 UI —— 这两步加起来
# 人工约 30 秒。RPC 服务由 QMT 的 init/handlebar/adjust 回调驱动，
# 实例没挂 = 服务端永远不启动（redis 里看不到 bigqmt:* 队列）。
# 下面 [A]-[D] 即 deploy-user-guide.md §3.5–3.6 的浓缩版。
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
        `$env:BIGQMT_ACCOUNT_ID='$AccountId'
        qmt-server --trader-backend bigqmt
    详见仓库根 deploy-user-guide.md（§3.5 起为手工步骤，§7 为故障排查）
"@ -ForegroundColor White

Write-Host "`n部署自动化部分全部完成 ✔  (下单门控 rpc_allow_order_methods=False 保持关闭)" -ForegroundColor Green
