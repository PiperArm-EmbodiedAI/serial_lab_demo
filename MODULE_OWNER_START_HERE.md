# 模块接入说明

适配器负责注册、领取任务、心跳和结果上报。业务模块提供单次任务执行入口，并在实际完成后返回。

## 1. 准备接入信息

- 中央服务器地址，例如 `http://192.168.1.100:8765`。
- 模块编号 `node_id`、步骤编号 `step_id` 和执行顺序 `order`。
- 业务程序路径、Python 解释器路径及任务超时时间。

`node_id`、`step_id` 分别全局唯一，使用英文字母、数字、下划线或连字符；`order` 为全局唯一正整数。显示名称可使用中文。

## 2. 配置执行入口

已有脚本使用 `configs/command_module.json`。示例中的地址、路径、命令和顺序需替换为实际值：

```json
{
  "server_url": "http://192.168.1.100:8765",
  "node_id": "camera_module",
  "state_dir": "../runtime/camera_module",
  "steps": [{
    "step_id": "inspect_container",
    "name": "容器检查",
    "order": 20,
    "python": "/opt/conda/envs/vision/bin/python",
    "cwd": "/opt/inspection",
    "command": ["{python}", "run_inspection.py", "--mode", "once"],
    "params": {},
    "timeout_seconds": 300
  }]
}
```

| 字段 | 说明 |
|---|---|
| `server_url` | 中央服务器 HTTP 地址 |
| `node_id` | 模块适配器编号（标识一个模块） |
| `state_dir` | 执行状态目录，每个适配器独立使用 |
| `steps` | 注册的步骤列表，可包含多个步骤 |
| `step_id` / `name` | 步骤编号 / 显示名称（一个模块可以承担多个步骤在不同的order顺序执行） |
| `order` | 执行顺序，按数值升序排列 |
| `python` | 业务解释器；省略时使用适配器的解释器 |
| `cwd` | 业务工作目录 |
| `command` | 命令参数数组；`{python}` 替换为业务解释器 |
| `params` | 输入参数字典 |
| `timeout_seconds` | 任务时限，包含初始化耗时 |

`cwd`、`state_dir` 的相对路径以配置文件目录为基准。`command` 不解析 shell 语法；执行 shell 脚本使用 `["bash", "/opt/inspection/run_once.sh"]`。

脚本需等待实际任务完成后退出：退出码 `0` 表示成功，非 `0` 表示失败。调用常驻 HTTP/ROS 服务时，脚本需等待服务返回完成结果。

## 3. 启动与确认注册

在工程根目录执行：

```bash
python node.py --config configs/command_module.json
```

在中央页面确认模块显示“在线”、步骤和顺序正确。启动实验后，适配器按调度执行任务。修改配置后重启适配器；活动流程结束前保持配置不变。

## 其他执行入口

### Python 函数

使用 `configs/my_module.json`，修改地址、编号和顺序，在 `examples/my_task.py` 的 `execute(context)` 中接入业务调用。`function` 格式为 `../examples/my_task.py:execute`，文件路径相对于配置目录。

```bash
python node.py --config configs/my_module.json
```

成功返回 JSON 字典或 `None`；失败抛出异常。`False` 不作为失败标志。函数在子进程内执行，每次任务结束后退出。

假设已有业务函数：

```
def func(a, b, c, d, e, f, g):
    ...
```

配置写：

```
"params": {
  "a": "E:/models/detector.pt", # 都可以
  "b": 0.8,
  "c": 3,
  "d": 4,
  "e": 5,
  "f": 6,
  "g": 7
}
```

此外目前需要增加一个小的适配函数，原业务函数可以保持不变，**params一定要用字典调用不要直接用，也就是说可执行函数尽量都用一个字典传入单个参数然后从字典查传参**：

```
def func(a, b, c, d, e, f, g):
    # 原有业务
    ...


def execute(context): # 随便什么名字都可以
    result = func(**context["params"])
    return {"result": result}
```

然后配置文件指向：

```
"function": "../business.py:execute" # 记得保持一致
```

### 常驻模型或设备

参照 `examples/embedded.py` 和 `configs/embedded.json`，初始化完成后通过 `run(config_path, handlers={"existing_action": execute})` 注册处理函数，配置中设置 `"handler": "existing_action"`。

`run()` 持续阻塞，处理函数在业务线程执行。要求主线程调用的设备库需另行适配。每个步骤的 `command`、`function`、`handler` 三选一。

例如，假设程序可能这样写：

```
model = load_model()       # 加载模型
result = model.detect()   # 执行检测
```

接入 `embedded` 后变成：

```
from labflow.agent import run

# 程序启动时只执行一次
model = load_model()


# 每收到一次任务，就调用一次这个函数
def execute(context):
    result = model.detect()
    return {"result": result}  # 返回值需能转换为 JSON


# 注册函数，然后持续等待中央服务器的任务
run(
    "configs/embedded.json",
    handlers={"existing_action": execute}, # 别忘了绑定函数
)
```

这里的 `load_model()` 和 `model.detect()` 是示意，需要替换为实际模型代码。

config的 embedded.json 配置中的：

```
"handler": "existing_action"
```

就表示：**这个步骤收到任务后，调用上面绑定的 `execute` 函数。** `existing_action` 是用于匹配的名称，随你写什么，配置和代码中保持一致即可。**params字段的用法和上一个方案保持一致也是需要传入参数然后用字典调用。**

因此部署时，启动的是这个业务程序，例如：

```
python vision_service.py
```

它既保留模型，又负责等待调度。关闭这个程序，模型和调度接入也就一起结束。

## 参数与结果

函数通过 `context["params"]` 获取配置参数，通过 `context["previous_results"]["前序step_id"]` 获取前序结果；`context` 同时包含 `run_id`、`task_id` 和 `step_id`。

命令模式通过环境变量 `LABFLOW_TASK_FILE` 指向的 JSON 文件读取上述输入。需要传递结果时，将 JSON 字典写入 `LABFLOW_RESULT_FILE` 指向的文件；无输出数据时可省略。

结果上限 64 KiB。图片和大数组保存到共享位置，返回可访问路径。参数及结果字段、单位和成功条件应在模块间保持一致。
