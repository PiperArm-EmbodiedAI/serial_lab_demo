# HTTP 接口说明

使用 `node.py` 时由适配器处理以下协议。其他语言或自定义客户端可直接调用。

默认端口 `8765`，无需认证请求头。请求与响应使用 JSON，请求体上限 256 KiB，正常响应为 HTTP 200。

## 接口列表

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 服务存活检查 |
| POST | `/api/register` | 注册模块及步骤 |
| POST | `/api/poll` | 心跳及任务领取 |
| POST | `/api/events` | 上报执行状态 |
| GET | `/api/plan` | 注册清单、在线状态和计划版本 |
| POST | `/api/runs` | 开始流程 |
| GET | `/api/status` | 最近流程及简要历史 |
| GET | `/api/runs/{run_id}` | 指定流程状态 |
| POST | `/api/runs/{run_id}/pause` | 立即暂停后续派发（不取消已派发任务） |
| POST | `/api/runs/{run_id}/pause-after-current` | 当前已派发步骤完成后暂停 |
| POST | `/api/runs/{run_id}/resume` | 恢复已暂停流程 |
| POST | `/api/runs/{run_id}/close` | 关闭流程记录 |
| DELETE | `/api/nodes/{node_id}` | 无活动流程时删除离线模块 |

错误响应为 `{"error":"说明"}`。状态码：400 参数错误、403 模块不匹配、404 未找到、409 状态或注册冲突、413 数据过大。

## 1. 注册模块

POST `/api/register`：

```json
{
  "node_id": "module_a",
  "instance_id": "instance_a",
  "config_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "busy": false,
  "steps": [{
    "step_id": "step_a", "name": "步骤A", "order": 10,
    "params": {}, "timeout_seconds": 600
  }]
}
```

- `instance_id` 每次进程启动重新生成；`config_hash` 替换为实际执行配置的 SHA-256 十六进制摘要。
- 各编号使用 ASCII 字母、数字、点、下划线或连字符，长度 1–80，以字母或数字开头。
- `step_id` 和 `order` 全局唯一；一次注册替换该模块的完整步骤集合。
- 同一模块的在线实例不可重复注册为另一实例；离线实例被替换时，已领取的流程会阻断。

## 2. 心跳与领取

POST `/api/poll`，默认每秒调用一次，心跳租约默认 15 秒：

```json
{"node_id":"module_a","instance_id":"instance_a","busy":false}
```

无可执行任务时返回 `task:null`。领取成功的响应示例：

```json
{
  "run_status": "running",
  "task": {
    "run_id": "run_001", "task_id": "task_001", "step_id": "step_a",
    "params": {}, "previous_results": {},
    "config_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "deadline": 1780000000.0, "expires_in_seconds": 599.9
  }
}
```

`previous_results` 按前序 `step_id` 索引。执行前核对配置摘要并持久记录 `task_id`；重复领取同一任务只查询或重报已有结果，不重复执行。

`deadline` 为服务器超时时刻；客户端使用 `expires_in_seconds` 和本地单调时钟判断启动有效期。执行期间及最终事件未获确认前保持 `busy:true`。进程重启遇到未完成记录时上报 `unknown`。

## 3. 上报状态

POST `/api/events`：

```json
{
  "node_id": "module_a", "instance_id": "instance_a",
  "event_id": "event_001", "run_id": "run_001", "task_id": "task_001",
  "status": "succeeded", "result": {"value": 1}, "error": null
}
```

`status` 支持 `running`、`succeeded`、`failed`、`unknown`。成功结果使用 `result`，失败原因使用 `error`；结果字典上限 64 KiB。

最终结果先持久化再上报。网络失败时保持同一 `event_id` 和事件内容重传；重复事件返回 `duplicate:true`。实例重启后 `instance_id` 可变化，冲突终态会被拒绝。

## 4. 开始与关闭流程

GET `/api/plan` 取得 `plan_version`，再 POST `/api/runs`。旧客户端可继续只传 `plan_version`，表示运行全部步骤；新客户端可以按 `step_id` 选择子集：

```json
{"plan_version":"当前计划版本", "step_ids":["step_a","step_c"], "mode":"selected"}
```

`step_ids` 必须非空、唯一且来自当前注册清单。服务器从当前计划构造快照并按 `order` 排序；只要求所选步骤所属模块在线且空闲，未选步骤所属离线/忙碌模块不阻止运行。省略步骤不保证其前置条件满足，单步运行也不会继承之前 run 的 `previous_results`。网页必须在提交前提示并要求操作者确认现场条件。

单步测试使用同一路径并传一个 ID 和 `mode:"single"`。run 保存 `mode`、`selected_step_ids`、`skipped_steps` 及冻结的 tasks，便于审计。`plan_version` 对完整注册清单校验，避免页面过期。

POST `/api/runs/{run_id}/close`：

```json
{"physical_checked":true,"note":"动作已结束，设备状态已核实"}
```

`pause-after-current` 在有已派发/运行中的任务时将状态设为 `pausing`；当前任务仍可完成，成功后变为 `paused`，失败/未知/超时仍会阻断。若当前任务尚未派发，则立即进入 `paused`。旧 `pause` 仍立即停止待派发任务的领取，但不会取消已经派发的任务。暂停和关闭仅修改调度状态，不停止设备。失败、超时或状态未知时阻断流程；迟到结果不会自动恢复执行。

服务端 SQLite 状态文档现带 `schema_version`，启动时从旧版未标记状态自动迁移，保留节点注册与历史记录。部署更新时保留相同的 `--state-dir`；GitHub 只更新代码，不迁移数据库。任意可访问主机可运行中央 Server，但同一时刻只应有一个活动 Server；Node 的 `server_url` 必须指向该实例。
