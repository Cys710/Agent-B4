# B4 模块阅读指南

B4 的对外职责是把一组 messages 和 tools schema 转成标准 AIMessage。旧入口
`code/b4_local_agent_llm.py` 仍然保留，供 B1、B5、测试脚本和命令行继续导入。
具体实现拆在本目录。为了避免过度拆分，B4 现在按“生成、规划执行、服务入口、评估/CLI”几块组织：

| 文件 | 职责 |
|---|---|
| `service.py` | B4 核心入口：`generate_ai_message`、schema passing 对比 |
| `generation.py` | AIMessage 生成基础能力：mock、模型加载、prompt 组装、输出解析 |
| `planning.py` | plan-and-execute：生成计划、选择资源、执行任务、专家模型、最终回答合成 |
| `evaluation.py` | B4 批量工具调用评估 |
| `cli.py` | 命令行参数和 CLI 主函数 |

推荐阅读顺序：

1. 先看 `service.py` 的 `generate_ai_message`，理解 B4 主流程。
2. 如果是普通工具调用、mock、模型输出格式问题，看 `generation.py`。
3. 如果是计划执行、工具/专家模型选择、任务结果汇总，看 `planning.py`。
4. 如果是命令行或批量评估，看 `cli.py` 和 `evaluation.py`。
