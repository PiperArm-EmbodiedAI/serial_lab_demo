# 实验串行调度系统

通过 HTTP 连接中央服务和各模块，按 `order` 从小到大执行任务。当前步骤成功后启动下一步；失败或状态不确定时停止派发。

## 运行要求

- Python 3.9+，无需安装第三方包。
- 模块机器可访问中央服务器的 TCP 8765 端口。
- 单个中央服务、单条活动流程，各步骤串行执行。

以下命令均在工程根目录执行。

## 1. 启动中央服务

任意一台可被所有 Node 访问的机器都可以运行中央服务；同一时刻应只有一个活动中央 Server。Node 的 `server_url` 指向该机器。不同主机分别运行时不会自动共享状态，也不提供多主复制或故障切换。

```bash
python server.py --host 0.0.0.0 --port 8765 --state-dir runtime/server
```

升级代码时保留该 `--state-dir` 中的 SQLite 数据；GitHub 拉取只更新代码，不同步状态数据库。迁移到另一主机需先停止服务并一致性迁移状态，或接受空的新注册表。升级/重启前不要留有活动物理流程；重启会保守阻断活动 run。

浏览器打开 `http://中央服务器IP:8765`。页面自动连接，无需登录。

## 2. 配置并注册模块

将工程复制到模块机器，按[模块接入说明](MODULE_OWNER_START_HERE.md)修改配置。已有脚本使用 `configs/command_module.json`：

```bash
python node.py --config configs/command_module.json
```

适配器启动后自动注册、领取任务并报告结果，运行期间需保持进程在线。模块端无需开放端口。

配置中的 `server_url` 使用中央服务器实际 IP，例如 `http://192.168.1.100:8765`；`0.0.0.0` 仅用于服务监听。

## 3. 开始流程

网页步骤表默认全选，可取消勾选以在本次流程屏蔽某些步骤；被屏蔽步骤对应的 Node 可以离线，实际所选步骤对应的 Node 必须在线且空闲。也可对单个步骤使用“单步测试”。服务器按 order 对所选步骤排序并冻结本次清单。

跳过步骤/单步测试不会验证物理前置条件，也不会携带被跳过步骤的结果；仅在现场确认设备状态和依赖安全后执行。网页支持“当前步骤完成后暂停”，等待当前任务成功完成后停止派发，点击继续后再执行下一步。

- `node_id` 和 `step_id` 分别全局唯一；`order` 为全局唯一正整数，可使用 10、20、30。
- 一个模块可注册多个步骤；重复编号或顺序会拒绝注册。
- 修改配置后需重启适配器；参与活动流程的模块须在流程结束后修改。
- 模块离线后注册信息保留；新注册步骤在下次流程生效。

## 4. 查看与维护

```bash
python labctl.py plan
python labctl.py start --expect-steps 3
python labctl.py status
python labctl.py pause
python labctl.py resume
python labctl.py remove-node 模块的node_id
```

命令默认连接本机；远程操作在子命令前添加 `--url http://中央服务器IP:8765`。`--expect-steps` 校验注册步骤数量。

“立即暂停后续派发”不会取消已派发任务；“当前步骤完成后暂停”会等待当前任务返回成功，再暂停派发。两者都不是设备急停。新版本网页可“请求停止当前步骤”：升级后的 Node Agent 对命令/函数子进程尝试发送一次 SIGINT（Windows 为 CTRL_BREAK_EVENT），不会自动强杀；内嵌 handler 无法强制中断，旧版 Agent 也不会响应请求。停止不保证业务代码安全清理硬件，必须现场核查。

流程阻断后，操作者可以在网页选择“人工确认完成并继续”或“跳过此步骤并继续”，均要求核实设备状态并填写记录。人工完成可选填 JSON 结果；未填写时后续步骤不会收到该步骤的 `previous_results`。失败、超时、断线或中央服务重启不会自动重试动作。确认动作结束后可关闭旧记录：

```bash
python labctl.py close --physical-checked --note "动作已结束，设备状态已核实"
```

适配器执行中退出后，未完成任务标记为 `unknown`。确认旧动作已停止，在适配器关闭时处理记录：

```bash
python node.py --config configs/command_module.json --resolve-unknown TASK_ID --note "旧动作已停止"
```

随后重启适配器，上报失败并关闭旧流程。状态目录保存执行记录和去重信息，恢复时需保留。设备停止由业务模块执行。

删除注册前，停止对应适配器，等待默认 15 秒离线判定，并确认无活动流程：

```bash
python labctl.py remove-node module_id
```

## 模拟演示

```bash
python run_demo.py
```

启动中央服务和三个模拟模块，打开终端显示的地址即可操作。Windows 可双击 `START-DEMO.cmd`。模拟任务不连接设备，Ctrl+C 结束演示。

## 文档与日志

- [模块接入说明](MODULE_OWNER_START_HERE.md)：配置字段和业务接入方式。
- [HTTP 接口说明](docs/PROTOCOL.md)：直接实现通信协议时使用。
- [验证记录](docs/VALIDATION.md)：已完成检查及验证范围。
- `runtime/`：运行状态；模块日志位于配置的 `state_dir/tasks/task_id/`。
