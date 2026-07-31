# Agent 思考过程展示优化

## 需求描述

当前 Agent 模式的思考过程面板展示太机械，只显示了"第1步：调用retrieve_knowledge → 第2步：调用generate_answer"，缺少 Agent 做决策时的"内心独白"。

需要改成：在每一步的工具调用**之前**，展示一段 Agent 的**思考/自言自语**内容，让用户看到 Agent 为什么调这个工具、对返回结果怎么判断、下一步打算怎么走。

## 预期效果

以前：
```
📋 第 1/2 步
🔍 调用retrieve_knowledge
📝 输入参数：{ "query": "退货政策" }
📎 工具返回：找到 3 条相关文档
```

现在：
```
📋 第 1/2 步
💭 思考：用户问的是退货政策，我需要先从知识库检索一下有没有相关的文档内容。
🔍 调用retrieve_knowledge
📝 输入参数：{ "query": "退货政策" }
📎 工具返回：找到 3 条相关文档
```

```
📋 第 2/2 步
💭 思考：检索到了3条关于退货政策的文档，内容比较完整，可以基于这些信息生成回答了。
🤖 调用generate_answer
📝 输入参数：{ "context": "...", "question": "退货政策是什么" }
📎 工具返回：生成回答完成
```

## 改动点

### 1. 后端 `agent/agent.py`

在 `run()` 方法中，DeepSeek 返回的 `message` 里有一个 `content` 字段——function calling 模式下，模型通常会在 `content` 里写它的思考过程（自言自语），然后 `tool_calls` 才是实际要调的工具。

当前代码（约第151-156行）只记录了 `action`、`input`、`output`，没有记录 `content`。

**改法**：在提取 `tool_call` 之前，先把 `message.content` 存下来，加到 `trace_step` 里新增一个 `reasoning` 字段。

关键代码位置：
- `agent/agent.py` 约第128行：`choice.get("message", {})` 之后，提取 `content`
- 约第151行：`trace_step` 字典里加 `"reasoning": reasoning`

### 2. 前端 `templates/agent.html`

在 `renderAgentTrace()` 函数中，每一步的展示顺序改为：

```
步骤标题 → 💭 思考内容（新加） → 工具调用名称 → 输入参数 → 工具返回
```

关键代码位置：
- `templates/agent.html` 约第660行：`html += '<div class="step-action ...'` 这一行**之前**，插入思考内容的渲染

思考内容的 HTML 结构参考：
```html
<div style="margin-bottom:6px;">
  <div class="step-detail-label">💭 思考</div>
  <div class="step-detail-text" style="color:#b0b0d0;font-style:italic;">思考内容</div>
</div>
```

注意：
- 如果 `reasoning` 为空或纯空白，不要显示思考区块
- 思考文字用斜体 + 浅色，和工具调用的正常文字区分开
- 用 `escapeHtml` 转义，防止 XSS

## 验证方式

1. 启动服务，进入 `/agent` 页面
2. 随便问一个问题
3. 展开 Agent 思考过程面板
4. 确认每一步的工具调用**之前**都有一段 💭 思考内容
5. 确认思考内容不是空的、不是"null"
