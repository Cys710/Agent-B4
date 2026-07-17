项目模块: B5 memory update
B5 第 3 个拓展点状态: 已完成
模型路径: /root/siton-tmp/20235870/assignment_B/Qwen3.5-4B
向量检索: 已完成
新增能力: 系统会根据新对话内容自动检索相关旧 memory，判断应该更新还是新增。
新增能力: 合并时会去除重复信息，把补充内容追加到 Merged Updates。
新增能力: 如果旧信息和新信息不一致，会写入 Conflict Notes，并按 prefer_new 策略标记新信息优先。
