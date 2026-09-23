# 双 Node 接入与联调说明

本文按当前仓库代码及 `labserver/LIFT2_SCHEDULER_HANDOFF.md` 整理。它描述的是**部署和联调流程**，不是已在两台真实设备上验证成功的配置。第一次真实设备运行必须由现场人员看护。

## 0. 先核对的关键事项

1. 中央调度服务在交接文档中写的是 `http://100.64.222.133:8765`。以下配置暂按此地址示例；请以当前实际运行的中央服务地址为准。两台 Node 都必须能访问该地址的 TCP 8765。`0.0.0.0` 只用于中央服务监听，不要写入 Node 配置。
2. 交接文档描述的 Lift2 接入入口为 `/home/arx/lift2_deploy` 下的 `lift2 node-up`，它注册 `order=30` 的准备步骤和 `order=50` 的实验/收尾步骤。**这与聊天中列出的原始 `lift2` 命令串并不完全相同**：交接文档写了高度 1，聊天命令写高度 0；准备与实验命令的拆分也不同。先在板子上核对实际 `scheduler` 配置/代码及最终安全动作，不能把两份说明混合后直接运行。
3. `labserver/run_demo.sh` 并非单纯在当前服务器本机执行一次实验：脚本默认会 SSH 到 `100.64.160.17` 上的 `sysu` 用户，再运行远端目录中的 `piper_demo_step.py`。这与作为 Node 的 `100.65.188.24` 不是同一 IP。确认这个转发目标、远端程序和权限是预期的；同时确认 `piper_demo_step.py` 确实存在于脚本要求的远端目录。
4. `run_demo.sh` 文件包含明文密码默认值/回退路径。**在清理这些默认值并安全配置认证前，不要把它用于无人值守调度。** 若之前提供的临时 SSH 密码是真实可用的，建议立即更换；不要把密码写进 Node JSON、脚本、命令行或日志。优先用受限 SSH key，并为需要的提权操作配置最小权限。
5. 调度器把业务进程的退出码 `0` 当作成功，并在前一步成功后才派发下一步。`run_demo.sh` 的成功是否等同于“另一 Node 的实体操作已完成且设备可安全交给 Lift2”，必须根据 `piper_demo_step.py` 实际行为确认。不要只根据脚本名或固定等待 150 秒判断完成。

## 1. 调度关系

两个模块各运行一个 `node.py` 适配器，注册到同一个中央服务；中央服务根据全局唯一 `order` 串行调度。适配器不要求开放入站端口。

| order | node | 建议步骤 | 完成条件 |
|---:|---|---|---|
| 30 | Lift2 Ubuntu/SOC 板 | 准备阶段：现场核验后清停、dry-run 检查路线、平台升至 17（以板上已验证实现为准） | Lift2 准备任务真实结束，且平台反馈核验通过后返回成功 |
| 40 | 第二台服务器 | 运行 `run_demo.sh` 指定参数 | 远端业务实际完成；脚本退出码 0 确实代表完成条件达成 |
| 50 | Lift2 Ubuntu/SOC 板 | 服务与状态检查、后续动作、实验路线和收尾（以板上已验证实现为准） | 所有步骤实际完成并返回成功 |

`order` 不是延时或优先级。order 30 成功后，调度器会派发 order 40；order 40 成功后才派发 order 50。因此“等第二个 Node”通过顺序依赖实现，不要在 Lift2 命令里用 `sleep` 猜等待时间，也不要把整条 Lift2 流程放进一个大 command 里。

交接文档还提到可选的 `order=1` 清理步骤。如果当前中央计划里已有该步骤，保留并核对；不要再注册重复 order。启动前要求两台 Node 都在线、计划完整、全局没有重复 `node_id`、`step_id` 或 `order`。

## 2. Node A：Lift2 板载 Ubuntu

### 2.1 先核实板端接入是否已经安装

通过 ToDesk 登录板子，在终端检查交接文档列出的文件是否存在：

```bash
ls -l /home/arx/lift2_deploy/scheduler/lift2_task.py \
      /home/arx/lift2_deploy/scheduler/lift2_node.json
```

核实配置中的 `server_url`、`node_id`、两个 step 的 ID/order、路线文件和实际高度。尤其确认 `height 0` 与交接文档的 `height 1` 哪一个是现场批准的安全终态。不要为了匹配表格自行修改真实运动逻辑。

如果板端文件与交接文档一致，按板端接入说明启动（这是交接文档给出的入口）：

```bash
conda activate base
cd /home/arx/lift2_deploy
lift2() { bash /home/arx/lift2_deploy/bin/lift2 "$@"; }
lift2 node-up
```

然后在板端检查中央计划：

```bash
python3 /home/arx/lift2_deploy/vendor/serial_lab_demo/labctl.py \
  --url http://100.64.222.133:8765 plan
```

`node-up` 是板端既有封装，不在当前仓库中；如果命令不存在或显示的 steps/orders 与上表不同，先停止，不要改用未经核验的命令串替代。确保适配器进程持续运行；断线/重启时不要直接重跑设备动作。

### 2.2 若板端尚未安装该封装

当前仓库里没有 `lift2_task.py`、`lift2_node.json` 或 Lift2 node-up 的实现，因此不能仅靠本仓库生成一个可靠的真实设备配置。先由 Lift2 维护者把设备动作拆为两个可独立执行、等待真实完成并验证设备反馈的入口：准备步骤 order 30 和后续步骤 order 50。每个步骤失败必须返回非零/异常；不要把只表示命令已发出的返回值当成动作完成。

## 3. Node B：第二台服务器

### 3.1 先检查运行脚本

在第二台服务器（用户提供的 `100.65.188.24`）上，通过已授权的登录方式进入 `labserver` 目录。不要把密码写入命令或配置。先确认：

```bash
cd /实际路径/labserver
ls -l run_demo.sh piper_demo_step.py
```

`piper_demo_step.py` 可能位于脚本将要 SSH 登录的另一台机器，而不是 `100.65.188.24`。请按 `run_demo.sh` 中的 `ARM_HOST`、`ARM_DIR` 和认证设置确认实际执行位置。脚本注释里的端口参数被传给 Python 程序；它不是中央调度器的端口 8765。需要由脚本维护者确认 8001 的用途、是否需监听/开放防火墙、以及任务结束时的判定方式。

先移除脚本中的明文密码默认值，采用受限 key 认证；确认脚本非交互运行、失败时非零退出、成功时只在业务确实完成后返回 0。适配器超时不会让物理设备安全停止。若业务可能运行超过配置超时，要先调整超时并验证停止/异常处理机制。

### 3.2 配置第二个 Node

将此工程部署到服务器，例如 `/home/arx/serial_lab_demo`。新建 `/home/arx/serial_lab_demo/configs/labserver_node.json`（路径可按实际部署修改），示例如下。`cwd` 必须改成 `run_demo.sh` 实际所在目录；`state_dir` 使用该 Node 独占的持久目录：

```json
{
  "server_url": "http://100.64.222.133:8765",
  "node_id": "piper_labserver",
  "state_dir": "/home/arx/serial_lab_demo/runtime/piper_labserver",
  "steps": [
    {
      "step_id": "piper_demo_run",
      "name": "Piper 实验任务",
      "order": 40,
      "cwd": "/实际路径/labserver",
      "command": [
        "bash", "run_demo.sh",
        "--port", "8001",
        "--seconds", "150",
        "--replan-steps", "16",
        "--gripper-step", "0.20",
        "--tracking-error", "0.70",
        "--joint-step", "0.15"
      ],
      "params": {},
      "timeout_seconds": 300
    }
  ]
}
```

`timeout_seconds: 300` 是留出 SSH/初始化开销的示例值，不代表 300 秒超时会停止远端动作；需按测得的最大时长设定，并先验证超时后的人工处置。不要给两个适配器共用 `state_dir`。

### 3.3 启动并确认注册

```bash
cd /home/arx/serial_lab_demo
python3 node.py --config configs/labserver_node.json
```

保持适配器终端或受控服务进程运行。再从任一可连接中央服务的机器检查：

```bash
python3 labctl.py --url http://100.64.222.133:8765 plan
python3 labctl.py --url http://100.64.222.133:8765 status
```

确认 `piper_labserver` 在线，且计划里只有预期的 order 40 步骤。不要先点“开始”来测试注册。

## 4. 首次联调检查表

开始真实实验前逐项确认：

- [ ] 中央服务健康；两台 Node 都显示在线，心跳稳定。
- [ ] 中央计划确切包含 order 30 → 40 → 50；步骤名、归属 Node 与 order 均正确；没有重复顺序。
- [ ] Lift2 路线、起始位置、平台高度、末态高度与现场批准的流程一致；清停动作由现场人员确认后执行。
- [ ] 第二台的 SSH 目标、运行目录、脚本依赖、认证方式及 8001 的含义已核实；远端 `piper_demo_step.py` 存在。
- [ ] order 40 的返回成功条件明确代表需要的另一 Node 操作已真实完成，而非只启动了异步服务。
- [ ] 指定现场观察员，确认急停/制动方法和安全区域；首次运行不无人值守。
- [ ] 先在不驱动物理设备的条件下验证脚本参数、路径和退出码；dry-run 不等于真实动作安全。

核对通过后，使用中央页面点击“开始一次实验”，持续观察页面及两端日志。不要在同一设备上同时运行另一套会控制底盘/机械臂的程序。

## 5. 异常处置

- 调度器失败、超时、Node 离线或状态 `unknown` 时，**不要直接重启任务或开始新流程**。框架不会自动重试，也不会替设备执行急停。
- 现场先用设备本身的安全停止方式确认所有动作结束，再检查设备状态；之后才关闭中央流程记录并按项目流程处理 unknown。
- 暂停只阻止后续步骤派发，不会取消已派发/正在运行的命令。
- 不要删除或清空适配器的 `state_dir`；它用于持久化任务去重与执行状态。
- 当前仓库的中央 HTTP 页面没有登录认证。只应在受信任的隔离网络中开放，并按现有部署策略限制访问；不要将 8765 直接暴露到公网。

## 6. 凭据处理

之前对话中出现过临时 SSH 凭据。若其仍有效，请立即轮换，并检查相关 shell 历史、聊天/工单记录及日志是否需要清理。此手册不保存或复述密码。不要提交私钥、密码、askpass 文件或含凭据的环境转储到版本库。
