import time


def execute(context):
    params = context["params"]
    a = params["a"]
    b = params["b"]

    time.sleep(2)  # 模拟任务执行

    return {"sum": a + b}