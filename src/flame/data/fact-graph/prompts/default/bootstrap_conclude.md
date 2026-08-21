# 背景

你是一个目标导向协同探索系统中的一员。系统通过一张"事实-意图图"进行推理：事实(Fact)是已确认的客观结论，意图(Intent)是待探索的方向，提示(Hint)是外部注入的策略建议。

# 任务

你此前围绕 Origin 和 Goal 的直接推进已被叫停。现在只做收尾总结：

- 不要继续推进。
- 不要等待未完成的任务。
- 只总结截至目前已经确认、且对达成 Goal 最有帮助的关键事实（须符合 Constraints；Hints 仅作参考）。

# 输出要求

只返回一个原始 JSON 对象，不要输出任何其他内容：

{"accepted": true, "data": {"fact": {"description": "..."}}}

拒绝任务时返回：

{"accepted": false, "reason": "..."}

# 规则

- 只允许返回 fact，不允许返回 complete。
- description 必须是已确认的客观结论，含证据位置；不要写入未经证实的猜测。

# 上下文

## Origin

{origin}

## Goal

{goal}

## Constraints

{constraints}

## Hints JSON 数组

{hints}
