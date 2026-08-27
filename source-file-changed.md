# 本次会话改动的源码与文件清单（source-file-changed.md）

> 范围：2026-08-27 晚 §6 步骤 2 部署会话（resume 自评审会话之后）。
> 结论先行：**仓库源码只改了 1 个测试文件**（7 增 1 删，commit `9fdcc50`）；
> 其余均为「新增文件」（文档、部署产物、运维脚本），**没有修改任何既有
> 生产代码或 QMT 客户端自带文件**。

---

## 1. 仓库内（big-qmt 分支，工作树干净）

### 1.1 源码修改（仅 1 处）

| 文件 | commit | 改动 |
|---|---|---|
| `tests/test_trading_backends.py` | `9fdcc50` | `test_install_shim_rejects_real_xtquant_dir` 封闭化：+1 行 `monkeypatch.setitem(sys.modules, "bigqmt_signal_trader", None)`，+说明文档串 |

**原因**：步骤 2 需在本机 pip 安装 `xtquant-big-convert`（部署来源 + 客户端运行时），
其顶层 `xtquant/` shim 使 `install_bigqmt_xtdata_shim` 的 site-packages 回退命中，
该用例断言 `is None` 随环境翻转失败。置 `sys.modules` 条目为 `None` 即模拟
「未安装」，恢复 56/56。产品代码（`src/qmt_bridge/**`）**零改动**。

### 1.2 新增文档

| 文件 | commit | 内容 |
|---|---|---|
| `deploy-user-guide.md` | `437644f` | bigqmt 部署与使用用户指南（本次会话产出） |
| `source-file-changed.md` | （本文件） | 改动清单 |

### 1.3 前一会话遗留（非本次，供对照）

`c300d38`（§6 步骤 1：`server/trading/{backend,mini_backend,bigqmt_backend,manager}.py`、
`bigqmt_shim.py`、`deploy_bigqmt_server.py`、`tests/test_trading_backends.py`、
`docs/big-qmt.md` 等）与 `c7ac428`（评审 6 项修复 + `docs/big-qmt-work-flow.md`）
均为上一评审会话提交，本次未触碰。

---

## 2. 仓库外 —— 我**编写**的代码（新文件）

| 文件 | 说明 |
|---|---|
| `C:\QMT_Simulator\python\bigqmt_rpc_bootstrap.py` | QMT 模型交易引导策略：导入 `bigqmt_signal_trader_redis_rpc_runtime`，绑定沙箱 `passorder/cancel/get_trade_detail_data`，暴露 `init/handlebar/adjust`。内容取自上游 runbook 的编辑器模板（改了 fallback 路径为 `C:/QMT_Simulator/python`），ASCII + `#coding:gbk`。**尚未在 QMT 内注册**（见 §5 未完成项） |
| `C:\Users\Docker\bigqmt\verify_rpc.py` | 步骤 2 客户端验证脚本：`configure()` → `connect()`(ping) → 资产/持仓/委托/成交四查询 → `order_stock` 预期被门控拒绝 |
| `C:\Users\Docker\bigqmt\clickq.ps1` / `rclickq.ps1` | 前台安全的前置点击/右击（自动 SetForegroundWindow，避免焦点被终端抢走） |
| `C:\Users\Docker\bigqmt\kb.ps1` | **raw `keybd_event`** 键盘助手（ctrla/ctrlv/ctrls/enter/esc/type）。SendKeys 进不了 Qt，物理级 keybd_event 可以 |
| `C:\Users\Docker\bigqmt\shot.ps1` / `cropdiff.ps1` | 截图；两图区域像素 diff（盲操作下的确定性反馈） |
| `C:\Users\Docker\bigqmt\edit2.ps1`、`edit_strategy.ps1`、`paste_run.ps1`、`scroll.ps1`、`click.ps1` | 编辑器自动化的一次性尝试脚本（保留供参考，多数已被 kb/clickq 流程取代） |
| `/tmp/py36_check.py`（即 `%TEMP%\1\`） | QMT 内置 Py3.6 导入自检脚本（redis + bigqmt_signal_trader） |

---

## 3. 仓库外 —— 部署/复制产生（非我编写）

| 位置 | 内容 | 来源 |
|---|---|---|
| `C:\QMT_Simulator\python\bigqmt_signal_trader\`（40 文件）+ 6 个顶层入口 | 上游 v0.2.9 服务端包 | `deploy_bigqmt_server.py` 从已装 wheel 复制 |
| `C:\QMT_Simulator\python\bigqmt_signal_trader_local_config.py` | 账户 88002471 + Redis 127.0.0.1:6379/db5 + `rpc_allow_order_methods: False` | 部署脚本按仓库内模板生成（含敏感信息，天然不入库） |
| `C:\QMT_Simulator\python\redis\` | redis-py 3.5.3 | TUNA 下载 wheel 解包复制（QMT Py3.6 纯 Python 依赖） |
| `C:\Users\Docker\bigqmt\shim\xtquant\` | xtquant import shim | 部署脚本 `--shim-out` 导出（供 `QMT_BRIDGE_BIGQMT_SHIM_DIR`） |
| `C:\Users\Docker\bigqmt\redis\` | Redis 5.0.14.1 服务端（tporadowski zip 解压） | api.github.com 资产通道下载 |
| `C:\Users\Docker\bigqmt\backup_期权网格.py`、`backup_网格策略.py.enc` | 尝试改写前的原始加密文件备份 | **实际未改写成功（ACL 拒绝），备份只是保险** |

---

## 4. 明确**没有**改动的东西（重要）

- `src/qmt_bridge/**` 全部产品源码 —— 零改动（步骤 1 的 PR 已在 `c300d38` 定稿）。
- `C:\QMT_Simulator\python\` 下 **QMT 自带的任何策略 `.py`**（网格策略、期权网格、
  双均线实盘示例PY 等）：ACL 为 Users 只读，覆盖尝试即被系统拒绝；
  文件 mtime 仍为 2026-06-16，内容未变。编辑器内的粘贴均**未保存**。
- `C:\QMT`（实盘客户端）、`C:\国金证券QMT交易端`、`C:\QMT_Simulate` —— 未触碰。
- QMT 客户端配置 / userdata —— 除正常日志外未写入。

## 5. 环境与进程状态变化（非文件改动）

- py312 用户 site-packages 新装：`xtquant-big-convert 0.2.9`、`redis 8.1.0`（TUNA）。
- 新增常驻进程：`redis-server.exe`（127.0.0.1:6379，Hidden 窗口，重启后需手动拉起）。
- QMT 客户端 `C:\QMT_Simulator\bin.x64\XtItClient.exe` **未被重启**；
  过程中误开过一次 Edge（已 `taskkill`），QMT 当前停在内置策略列表页，状态正常。

## 6. 遗留未完成（步骤 2 收尾）

`bigqmt_rpc_bootstrap.py` 尚未通过 QMT 编辑器「保存」写入策略索引（自动化卡在
保存对话框提交），因此模型交易实例未建、RPC 服务未启动。两条收尾路径见
`deploy-user-guide.md` §3.5–3.6（手工 30 秒）或经确认后重启客户端重建索引。
