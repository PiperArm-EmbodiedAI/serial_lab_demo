# Lift2 与中央串行调度系统联调交接说明

## 1. 当前结论

Lift2 侧的调度接入已经完成，可以和中央调度服务进行受控联调。

目前已经完成：

- Lift2 调度任务函数已封装为“准备阶段”和“实验阶段”两个步骤；
- 两条底盘路线已接入并通过 `--dry-run` 检查；
- 实验高度固定为 13，实验结束后平台降到 1；
- 准备阶段会把平台升到 17，并在中央流程中等待下一个单位完成托盘放置；
- 实验阶段会检查三路相机和模型服务；
- Python 语法、JSON 配置、调度 Agent 初始化均已通过；
- 尚未进行真实的跨单位完整运行，首次联调必须由现场人员看护。

## 2. 设备服务器与中央服务

Lift2 设备服务器：

```text
HostName: 100.65.188.24
User: arx
Port: 22
IdentityFile: ~/.ssh/lift2_key
```

Linux/macOS 登录：

```bash
ssh -i ~/.ssh/lift2_key -o IdentitiesOnly=yes -p 22 arx@100.65.188.24
```

Windows PowerShell 登录：

```powershell
ssh -i "$env:USERPROFILE\.ssh\lift2_key" -o IdentitiesOnly=yes -p 22 arx@100.65.188.24
```

不要在群聊中直接发送私钥。调度方应通过安全渠道取得访问权限，或由管理员配置其 SSH 公钥。

中央调度服务：

```text
http://100.64.222.133:8765
```

## 3. Lift2 侧文件

```text
/home/arx/lift2_deploy/scheduler/lift2_task.py
/home/arx/lift2_deploy/scheduler/lift2_node.json
/home/arx/lift_teleop/routes/route_20260921_145236.json
/home/arx/lift_teleop/routes/route_20260921_172812.json
```

调度框架源码位于：

```text
/home/arx/lift2_deploy/vendor/serial_lab_demo
```

`serial_lab_demo.zip` 解压后的源码与当前 `vendor/serial_lab_demo` 源码一致，不需要替换运行中的调度框架。

## 4. 中央流程与步骤顺序

建议的全局流程如下：

| order | 步骤 | 执行方 | 作用 |
|---:|---|---|---|
| 1 | clean | 调度系统已有步骤 | 清理或初始化实验状态 |
| 30 | `lift2_prepare_platform` | Lift2 | 清除停止标记、检查收尾路线、启动平台并升到 17 |
| 40 | 另一个单位的模型步骤 | 另一个单位 | 把物体放到 Lift2 托盘，实际完成后才返回成功 |
| 50 | `lift2_station_experiment_return` | Lift2 | 检查服务、移动、双臂实验、回放收尾路线、降到 1 |

`order` 是全局串行顺序编号。调度器按数字从小到大执行，前一步成功返回后才派发下一步。它不是等待时间，也不是优先级。`step_id` 和 `order` 都必须在所有节点中全局唯一。

另一个单位必须把自己的步骤注册在 30 和 50 之间，通常使用 `order=40`。如果调度方使用其他编号，需要同步修改 Lift2 的 `lift2_node.json`，然后重启 Lift2 节点。

## 5. Lift2 两个步骤的行为

### `lift2_prepare_platform`，order=30

执行：

1. 清除停止标记；
2. 检查收尾路线格式，不发送底盘运动；
3. 启动或复用平台驱动；
4. 将平台升到高度 17；
5. 检查平台反馈是否在目标高度附近；
6. 返回成功并保持平台在高度 17。

该步骤成功后，中央调度器会把控制权交给另一个单位。清除停止标记属于安全状态复位动作，启动中央流程前必须由现场人员确认设备和运动区域安全。

### `lift2_station_experiment_return`，order=50

执行：

1. 检查两条路线文件；
2. 启动或复用相机和模型服务；
3. 检查 `camera_h`、`camera_l`、`camera_r` 三路话题和模型端口；
4. 回放：

   ```text
   /home/arx/lift_teleop/routes/route_20260921_145236.json
   ```

5. 执行 `lift2 auto --height 13`；
6. 回放：

   ```text
   /home/arx/lift_teleop/routes/route_20260921_172812.json
   ```

7. 执行 `lift2 height 1`；
8. 返回实验结果和日志路径。

任一步失败都会抛出异常，中央调度器会将流程标记为失败并阻断后续步骤，不会自动重试。

## 6. 启动 Lift2 节点

在 Lift2 服务器执行：

```bash
conda activate base

lift2() { bash /home/arx/lift2_deploy/bin/lift2 "$@"; }

cd /home/arx/lift2_deploy
lift2 node-up
```

节点启动后会注册两个 Lift2 步骤。查看中央计划：

```bash
python3 vendor/serial_lab_demo/labctl.py \
  --url http://100.64.222.133:8765 \
  plan
```

启动中央流程前，应确认 Lift2 节点、另一个单位节点以及中央服务均在线，并确认计划中存在 order=30、order=40、order=50。

## 7. 联调前现场检查

- 机器人处于移动路线录制时的起始位置和朝向；
- 底盘运动路径无遮挡；
- 没有同时运行 `teleop_gui.py` 或其他底盘控制程序；
- 双臂和夹爪状态正常；
- 相机、模型、平台和机械臂周围没有影响运动的人员或物体；
- 现场人员知道如何执行停止和断电处理；
- 另一个单位确认其步骤只有在物体实际放到托盘后才返回成功。

首次联调建议由一名人员观察机器人，另一名人员观察中央页面和日志。

## 8. 监控与异常处理

查看中央流程：

```bash
python3 vendor/serial_lab_demo/labctl.py \
  --url http://100.64.222.133:8765 \
  status
```

查看 Lift2 节点日志：

```bash
ls -t /home/arx/lift2_deploy/logs/scheduler_node_*.log | head -1
```

紧急停止时使用：

```bash
lift2 stop
```

底盘仍在发送控制时使用：

```bash
lift2 brake
```

停止后不要直接重新开始流程。先确认现场安全、检查设备状态，再由调度方关闭被阻断的中央流程，并从新的流程重新开始。

如果节点在任务执行中重启，调度器可能把任务标记为 `unknown`。必须现场确认旧动作已经停止后，才能使用调度器提供的 `resolve-unknown` 流程处理，不得直接重复启动。

## 9. 日志位置

节点日志：

```text
/home/arx/lift2_deploy/logs/scheduler_node_*.log
```

调度任务日志：

```text
/home/arx/lift2_deploy/runtime/labflow/tasks/<task_id>/stdout.log
```

路线和自动实验日志：

```text
/home/arx/lift2_deploy/logs/scheduler_<run_id>_<task_id>/
```

## 10. 测试结论

Lift2 侧的软件封装和调度配置已经完成，可以进入中央服务联调。当前需要调度方完成的工作是：

1. 确认另一个单位的步骤使用 order=40 或提供最终编号；
2. 确认所有节点在线；
3. 检查中央计划顺序；
4. 在现场安全检查通过后启动一次完整流程；
5. 根据日志确认准备、托盘放置、移动、双臂实验和降台均成功。

第一次运行属于真实设备联调，不能把它当作纯软件测试。任何失败都应先停机和现场确认，再关闭中央流程并重新开始。

# Address

/home/arx/lift2_deploy/test
