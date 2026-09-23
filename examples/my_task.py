"""负责人只需要改 execute() 里面的业务调用。这个模板默认仅打印。"""


def execute(context):
    params = context["params"]
    # previous = context["previous_results"]  # 前面所有成功步骤的结果
    # result = your_existing_function(**params)
    # 必须等待实际动作完成并检查成功条件，再 return。
    # 失败请 raise RuntimeError("失败原因")。
    print("请在 examples/my_task.py 中接入你的任务，参数:", params)
    return {"demo_only": True, "message": "此模板未接入设备"}
