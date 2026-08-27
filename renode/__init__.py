"""
renode — Renode 模拟器后端

作为 Harness 的可选 backend，替代 monitor_client.py (OpenOCD + 真硬件)。

用法:
    embed_harness.py --backend renode --scenario xxx.yaml

后端接口 (与 monitor_client.py 一致):
    start()
    load_firmware(path)
    reset_and_run()
    read_variable(name) -> int
    read_variable_32(name) -> int
    sample_multiple(vars, duration, interval) -> dict
    close()
"""
