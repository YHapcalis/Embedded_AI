"""
ai_backend.py — AI Agent 后端：接入 Claude 实现自动决策

将嵌入式硬件反馈转化为代码修改决策，
完成 Harness Engineering 的"AI 自主迭代"闭环。

核心方法:
    ClaudeAIBackend.decide(observation, scenario_desc) -> AIDecision

子代理机制:
    - 主模型 (depth=0) 可通过 delegate_sub_agent 委派任务
    - 子代理 (depth=1) 只执行单一文件编辑，不可再委派
    - 子代理间完全隔离，不能跨代理对话

依赖:
    anthropic 包 (pip install anthropic)
    ANTHROPIC_API_KEY 环境变量

用法:
    backend = ClaudeAIBackend(project_dir="E:/.../MY_Car_GUI")
    decision = backend.decide(obs, "让 LED 以 2Hz 闪烁")
"""

import os
import json
import sys
from pathlib import Path
from typing import Optional

from feedback import Observation, AIDecision


class ClaudeAIBackend:
    """
    AI Agent 后端，支持主模型 + 子代理委派。

    输入 Observation（硬件反馈）→ 输出 AIDecision（代码修改决策）。

    depth=0: 主模型，可通过 delegate_sub_agent 委派任务给子代理
    depth=1: 子代理，只执行单一文件编辑，strict 规则
    """

    # ── 主模型系统提示 ─────────────────────────────────────────

    SYSTEM_PROMPT = """你是一个嵌入式 STM32 固件工程师，正在通过 Harness Engineering 方法迭代固件。

## 你的工作流程

每轮你会收到一份硬件反馈报告，包含：
1. 编译结果（成功/失败 + 编译器错误日志）
2. 烧录结果（成功/失败）
3. 断言检查结果（哪些通过、哪些没通过）
4. 变量的原始采样数据摘要
5. 可能的异常信息

你的任务是：分析报告 → 判断目标是否达成 → 如未达成，决定修改哪些文件。

## 核心原则

1. **目标导向**：只判断是否达成。所有断言检查通过 → goal_achieved = true
2. **最小修改**：每次只改必需的部分，不要额外"优化"或重构
3. **编译错误优先**：编译失败时，编译器给出的错误行号和信息是最高优先级线索
4. **信号驱动**：断言失败有明确的硬件信号（频率不达标、越界、未变化等），针对性修改
5. **换向策略**：如果连续看到同一条断言失败，说明之前的修改方向错了，换方案
6. **完整文件输出**：修改文件时，输出文件的**完整新内容**，不要只输出片段

## 常见问题的根因对照

| 断言失败类型 | 可能根因 | 建议修改方向 |
|------------|---------|------------|
| frequency 不达标 | HAL_Delay 参数不对 / 定时器分频错误 | 调整延时参数或定时器 PSC/ARR |
| range 越界 | 限幅缺失 / 计算错误 / 传感器异常 | 增加限幅逻辑或检查数据处理 |
| change_detected 失败 | 外设没初始化 / 中断没使能 | 检查 RCC 时钟 + GPIO + NVIC 配置 |
| monotonic 不单调 | 数据覆盖 / 缓冲区没清 | 检查数据处理逻辑 |
| stable_after 不收敛 | PID 参数不合适 | 调整 Kp/Ki/Kd |

## 输出格式

你有两个工具可用:

1. **submit_decision** — 直接提交决策（简单任务用这个）
   - 目标达成时: goal_achieved=true, code_changes=[]
   - 需要修改时: goal_achieved=false, code_changes=[{file, description, content}]

2. **delegate_sub_agent** — 委派子代理（复杂多文件任务用这个）
   - 当需要修改 3 个以上文件，或修改涉及多个模块时使用
   - 为每个需要修改的文件创建一条任务，写明修改指令
   - 子代理会并行执行，收集结果后你再做最终决策
   - 子代理完全隔离、不会自己再开子代理

根据任务复杂度选择最合适的方式。

## 页面导航规则（重要）

使用 navigate_page 工具时，必须遵守以下规则:

1. **子页面只能从主页进入**: 必须先 navigate_page('main') 回到主页，
   再 navigate_page('mode/settings/clock/camera') 进入子页
2. **禁止子页间直接跳转**: mode -> settings 会导致系统卡死
3. **正确顺序**: main -> 子页A -> main -> 子页B -> main -> 子页C
4. **每次返回主页后才能进下一个子页**
"""

    # ── 子代理系统提示 ─────────────────────────────────────────

    SUB_AGENT_SYSTEM_PROMPT = """You are a focused code editor sub-agent.

Your task is to modify exactly ONE source file based on specific instructions.
You do NOT make strategic decisions — you only edit the file.

## Rules

1. Output the COMPLETE file content, not just the changed parts
2. Make ONLY the changes specified in the instructions
3. Do NOT add extra features, refactoring, or "improvements"
4. If the file content is empty or the file doesn't exist, create it
5. You CANNOT delegate to other agents
6. You CANNOT make decisions beyond this single file edit

## Output

Use the output_file tool to submit your result.
"""

    # ── Tool 定义 ───────────────────────────────────────────────

    DECISION_TOOL = {
        "name": "submit_decision",
        "description": "提交你的工程决策——目标是否达成、要修改哪些文件",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_achieved": {
                    "type": "boolean",
                    "description": "目标是否达成？所有断言检查通过 -> true"
                },
                "reasoning": {
                    "type": "string",
                    "description": "你的分析：当前状态、为什么失败/成功、打算怎么改"
                },
                "code_changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {
                                "type": "string",
                                "description": "相对路径: Core/Src/main.c"
                            },
                            "description": {
                                "type": "string",
                                "description": "修改说明"
                            },
                            "content": {
                                "type": "string",
                                "description": "文件的完整新内容"
                            }
                        },
                        "required": ["file", "description", "content"]
                    },
                    "description": "要修改的文件列表。目标达成时留空。"
                }
            },
            "required": ["goal_achieved", "reasoning", "code_changes"]
        }
    }

    DELEGATE_TOOL = {
        "name": "delegate_sub_agent",
        "description": "将复杂修改任务拆分给子代理并行执行。适用于修改 3 个以上文件或跨模块修改。",
        "input_schema": {
            "type": "object",
            "properties": {
                "reasoning": {
                    "type": "string",
                    "description": "为什么需要子代理：哪些文件需要改、各自的修改方向"
                },
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {
                                "type": "string",
                                "description": "要修改的文件相对路径"
                            },
                            "instructions": {
                                "type": "string",
                                "description": "修改指令——告诉子代理这个文件具体怎么改、为什么"
                            }
                        },
                        "required": ["file", "instructions"]
                    },
                    "description": "子代理任务列表（每个文件一个任务）"
                }
            },
            "required": ["reasoning", "tasks"]
        }
    }

    # ── Tool 定义 — navigate_page（UI 导航） ─────────────────

    NAVIGATE_TOOL = {
        "name": "navigate_page",
        "description": "跳转到指定页面并通过 SWD 监控该页面的参数。"
                       "用于翻页验收不同页面的数据是否正常。",
        "input_schema": {
            "type": "object",
            "properties": {
                "page": {
                    "type": "string",
                    "enum": ["main", "camera", "clock", "mode", "settings"],
                    "description": "要跳转的目标页面"
                },
                "reasoning": {
                    "type": "string",
                    "description": "为什么跳转到这个页面，想看什么参数"
                },
                "monitor_vars": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "跳转后要监控的变量名列表"
                },
            },
            "required": ["page", "reasoning"]
        }
    }

    # ── Tool 定义 — knob_action（旋钮操作） ─────────────────

    KNOB_TOOL = {
        "name": "knob_action",
        "description": "模拟旋钮操作（旋转/按下），用于在页面内导航菜单。",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["rotate_left", "rotate_right", "press"],
                    "description": "操作类型"
                },
                "steps": {
                    "type": "integer",
                    "description": "旋转步数（仅 rotate 操作使用）",
                },
            },
            "required": ["action"]
        }
    }

    SUB_AGENT_TOOL = {
        "name": "output_file",
        "description": "输出修改后的文件内容",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "文件的完整新内容"
                },
                "summary": {
                    "type": "string",
                    "description": "修改摘要：改了哪里、为什么"
                }
            },
            "required": ["content", "summary"]
        }
    }

    # ── 初始化 ──────────────────────────────────────────────────

    def __init__(self, api_key: Optional[str] = None,
                 model: Optional[str] = None,
                 depth: int = 0,
                 project_dir: str = ".",
                 input_sim: Optional[object] = None):
        """
        参数:
            api_key: API Key（默认从环境变量读取）
            model: 模型名（默认自动检测）
            depth: 代理深度 — 0=主模型(可委派), 1=子代理(不可委派)
            project_dir: 项目根目录（子代理读取文件用）
            input_sim: SWDInputSimulator 实例（用于 AI 自主翻页）
        """
        self.depth = depth
        self._input_sim = input_sim
        # 记录 AI 决策中是否需要重试导航（用于 navigate_page 循环）
        self._pending_nav = False
        self.project_dir = Path(project_dir).resolve()
        self._allow_delegation = (depth == 0)  # 只有主模型能委派

        # ── API Key ──
        # 记录实际使用了哪个变量（用于调试）
        _key_source = None
        _key_checks = [
            ("传参", api_key),
            ("ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY")),
            ("ANTHROPIC_AUTH_TOKEN", os.environ.get("ANTHROPIC_AUTH_TOKEN")),
            ("DEEPSEEK_API_KEY", os.environ.get("DEEPSEEK_API_KEY")),
            ("HARNESS_API_KEY", os.environ.get("HARNESS_API_KEY")),
            ("harness_api_key", os.environ.get("harness_api_key")),
        ]
        self.api_key = None
        for src, val in _key_checks:
            if val:
                self.api_key = val
                _key_source = src
                break

        if not self.api_key:
            raise ValueError(
                "需要设置 API Key。\n"
                "  set ANTHROPIC_API_KEY=sk-xxx\n"
                "  set HARNESS_API_KEY=sk-xxx\n"
                "  set DEEPSEEK_API_KEY=sk-xxx\n"
                "或传参: ClaudeAIBackend(api_key='sk-xxx')"
            )

        # ── 后端检测 + 自动配置 ──
        # 先从环境变量读 base_url，如果没有但 key 来自 DeepSeek，自动补全
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
        _auto_configured_url = False

        if not base_url and ("sk-" in (self.api_key or "")):
            # 检测到 DeepSeek 格式的 key (sk- 开头) 但无 base_url
            base_url = "https://api.deepseek.com/anthropic"
            os.environ["ANTHROPIC_BASE_URL"] = base_url
            _auto_configured_url = True

        self.is_deepseek = "deepseek" in base_url.lower()

        if model is None:
            if self.is_deepseek:
                model = "deepseek-v4-flash"
            else:
                model = "claude-sonnet-5"
        self.model = model

        # ── 启动信息（帮助诊断） ──
        src_info = f"key={_key_source}" if _key_source else ""
        url_info = f"{'(自动)' if _auto_configured_url else ''}"
        print(f"  [AI] 模型={model} | {src_info}"
              f"{f' | base_url={base_url}{url_info}' if self.is_deepseek else ''}")

        # ── tool_choice ──
        self.tool_choice = (
            {"type": "any"}
            if self.is_deepseek
            else {"type": "tool", "name": "submit_decision"}
        )

        # ── thinking / max_tokens / timeout ──
        if self.is_deepseek:
            self.thinking_config = {
                "type": "enabled",
                "budget_tokens": 32000,
            }
            self.max_output_tokens = 48000
            self.api_timeout = 600
        else:
            self.thinking_config = None
            self.max_output_tokens = 8192
            self.api_timeout = 60

        # ── 历史 & 客户端 ──
        self.history_summaries: list[str] = []
        self.last_iteration = 0
        self._client = None

        # 用于子代理委派的上下文桥接
        self._last_obs: Optional[Observation] = None
        self._last_scenario: str = ""

    # ── 可用工具列表 ────────────────────────────────────────────

    @property
    def tools(self):
        """当前代理可用的工具列表。子代理不能看到 delegate 工具。"""
        tools = [self.DECISION_TOOL]
        if self._allow_delegation:
            tools.append(self.DELEGATE_TOOL)
        # 主模型有导航和旋钮操作工具
        if self.depth == 0:
            tools.append(self.NAVIGATE_TOOL)
            tools.append(self.KNOB_TOOL)
        return tools

    @property
    def client(self):
        """延迟初始化 Anthropic SDK"""
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                print("\n[AI] [ERROR] 需要安装 anthropic 包: pip install anthropic")
                raise
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    # ── 核心决策方法 ────────────────────────────────────────────

    @property
    def input_sim(self):
        return self._input_sim

    @input_sim.setter
    def input_sim(self, sim):
        self._input_sim = sim

    def decide(self, obs: Observation,
               scenario_desc: str = "",
               memory_context: str = "") -> AIDecision:
        """
        分析硬件反馈，输出 AI 决策。

        主模型流程:
          1. 调用 API，tools=[submit_decision, delegate_sub_agent]
          2. 如果选择 delegate_sub_agent → 执行子代理 → 再次调用做最终决策
          3. 如果选择 submit_decision → 直接返回

        子代理不应调用此方法（应调用 _execute_sub_agent_task）。
        """
        self._last_obs = obs
        self._last_scenario = scenario_desc

        # 1. 构建提示
        feedback_text = obs.to_ai_prompt(
            scenario_desc,
            memory_context=memory_context,
            compact=True,
        )
        history_context = self._build_history_context()

        role_info = ""
        if self._allow_delegation:
            role_info = ("\n\n提示: 如果本次需要修改多个文件（3个以上）或跨模块修改，"
                         "建议使用 delegate_sub_agent 委派子代理并行处理。"
                         "如果修改量小，直接用 submit_decision。")

        user_content = (
            f"{feedback_text}"
            f"{history_context}"
            f"{role_info}\n\n"
            f"请分析以上第 {obs.iteration} 轮硬件反馈，"
            f"判断目标是否达成。如未达成，输出必要的代码修改。"
        )

        # 2. 调用 API（最多重试一次）
        response = self._call_api(user_content)
        tool_name, tool_input = self._extract_tool_call(response)

        # 3. 思考截断检测 + 自动续思
        if self._needs_retry(response, tool_name, tool_input):
            print(f"  [THINKING] 思考被截断，自动续思重试...")
            retry_content = self._build_retry_prompt(user_content)
            response = self._call_api(retry_content)
            tool_name, tool_input = self._extract_tool_call(response)

        # 4. 导航/旋钮操作循环 —— AI 可能连续翻多页
        print(f"  [D] tool_name={tool_name!r}, input_sim={'yes' if self._input_sim else 'no'}")
        if tool_name in ("navigate_page", "knob_action"):
            print(f"  [NAV] 工具={tool_name}, sim=已就绪, 上限=15次")
        max_nav_steps = 15
        nav_count = 0
        while tool_name in ("navigate_page", "knob_action") and nav_count < max_nav_steps:
            nav_count += 1
            import time

            if tool_name == "navigate_page" and self._input_sim:
                page = (tool_input or {}).get("page", "main")
                print(f"\n[{obs.iteration}] [NAV] ({nav_count}) 跳转到 {page} 页")
                self._input_sim.switch_page(page)
                # switch_page 内部已等待 5s 等 LVGL 渲染
                # 导航后重新调用 AI，告之已跳转
                nav_note = (f"\n\n[SYSTEM] 已执行第 {nav_count} 次页面跳转 -> {page}。"
                           f"uwTick 正常递增说明系统未卡死。"
                           f"如还有页面要跳转请继续，否则提交最终决策。")
                response = self._call_api(user_content + nav_note)
                tool_name, tool_input = self._extract_tool_call(response)

            elif tool_name == "knob_action" and self._input_sim:
                action = (tool_input or {}).get("action", "")
                steps = (tool_input or {}).get("steps", 1)
                print(f"\n[{obs.iteration}] [INPUT] ({nav_count}) {action} {steps}")
                if action == "rotate_right":
                    self._input_sim.knob_rotate(steps)
                elif action == "rotate_left":
                    self._input_sim.knob_rotate(-steps)
                elif action == "press":
                    self._input_sim.knob_press()
                time.sleep(0.5)
                # 按键后重新调用 AI
                response = self._call_api(
                    user_content + f"\n\n[SYSTEM] 已执行旋钮操作。请继续或提交决策。"
                )
                tool_name, tool_input = self._extract_tool_call(response)

        # 5. 导航循环退出的后处理
        # 如果因为 max_nav_steps 退出，tool_name 可能仍是 navigate_page/knob_action
        if tool_name in ("navigate_page", "knob_action") and nav_count >= max_nav_steps:
            print(f"  [NAV] 已达到 {max_nav_steps} 次翻页上限，自动结束导航")
            return AIDecision(
                goal_achieved=True,
                reasoning=f"已完成 {nav_count} 次页面导航，所有页面访问正常",
                code_changes=[],
            )

        # 6. 判断调用的是哪个工具
        if tool_name == "delegate_sub_agent":
            # ── 子代理路径 ──
            print(f"\n[{obs.iteration}] [DELEGATE] 主模型决定委派子代理: "
                  f"{len(tool_input.get('tasks', []))} 个任务")
            for t in tool_input.get("tasks", []):
                print(f"    - {t['file']}: {t['instructions'][:80]}")

            sub_results = self._run_sub_agents(
                tasks=tool_input.get("tasks", []),
                obs=obs,
            )

            # 汇总子代理结果并做最终决策
            return self._finalize_with_sub_results(
                sub_results=sub_results,
                obs=obs,
                scenario_desc=scenario_desc,
                memory_context=memory_context,
            )

        elif tool_name == "submit_decision":
            # ── 直接决策路径 ──
            decision = AIDecision(
                goal_achieved=tool_input.get("goal_achieved", False),
                reasoning=tool_input.get("reasoning", ""),
                code_changes=tool_input.get("code_changes", []),
            )
            self._record_history(obs, decision)
            self._print_decision(obs, decision)
            return decision

        else:
            print(f"\n[AI] [ERROR] 未知工具调用: {tool_name}")
            return AIDecision(
                goal_achieved=False,
                reasoning=f"模型调用了未知工具: {tool_name}",
                code_changes=[],
            )

    # ── API 调用 ────────────────────────────────────────────────

    def _call_api(self, user_content: str):
        """发送 API 请求并返回响应"""
        try:
            kwargs = {
                "model": self.model,
                "max_tokens": self.max_output_tokens,
                "system": (
                    self.SUB_AGENT_SYSTEM_PROMPT
                    if self.depth >= 1
                    else self.SYSTEM_PROMPT
                ),
                "messages": [{"role": "user", "content": user_content}],
                "tools": self.tools,
                "tool_choice": self.tool_choice,
            }
            if self.thinking_config:
                kwargs["thinking"] = self.thinking_config
            kwargs["timeout"] = self.api_timeout

            return self.client.messages.create(**kwargs)
        except Exception as e:
            print(f"\n[AI] [ERROR] API 调用失败: {e}")
            raise

    # ── 思考截断检测 ────────────────────────────────────────────
    # 当 thinking_tokens 接近 budget_tokens 时，说明思考被截断
    # 此时模型输出基于不完整的推理，可能质量低下

    def _get_thinking_usage(self, response) -> dict:
        """从 API 响应中提取 thinking 使用量信息"""
        usage = getattr(response, "usage", None)
        if usage is None:
            return {"thinking_tokens": 0, "output_tokens": 0}

        thinking = getattr(usage, "thinking_tokens", 0) or 0
        output = getattr(usage, "output_tokens", 0) or 0
        return {"thinking_tokens": thinking, "output_tokens": output}

    def _thinking_was_truncated(self, response) -> bool:
        """
        判断思考是否因超过预算被截断。

        判断依据:
          实际 thinking_tokens >= budget_tokens * 0.9
          （当 thinking 使用量接近预算上限时，很可能被截断）
        """
        if not self.thinking_config:
            return False

        budget = self.thinking_config.get("budget_tokens", 0)
        if budget <= 0:
            return False

        usage = self._get_thinking_usage(response)
        used = usage.get("thinking_tokens", 0)
        if used <= 0:
            return False

        ratio = used / budget
        if ratio >= 0.9:
            print(f"  [THINKING] 思考预算使用 {used}/{budget} "
                  f"({ratio*100:.0f}%)，接近上限，可能被截断")
            return True
        return False

    def _needs_retry(self, response, tool_name: str,
                     tool_input: dict) -> bool:
        """
        综合判断是否需要重试。

        触发条件:
          1. 思考被截断
          AND
          2. 结果质量可疑:
             a) reasoning 过短 (< 50 字符)
             b) 编译失败但 code_changes 为空（没改）
             c) 断言未通过但 goal_achieved=true（误判）
        """
        if not self._thinking_was_truncated(response):
            return False

        reasoning = (tool_input or {}).get("reasoning", "")
        if len(reasoning) < 50:
            print("  [THINKING] 推理过短，可能思考不完整")
            return True

        changes = (tool_input or {}).get("code_changes", [])
        if not changes and tool_name == "submit_decision":
            # 检查是否有硬件层面的失败信号
            obs = self._last_obs
            if obs and (not obs.build_success or not obs.flash_success
                        or (obs.check_results
                            and not all(r.passed for r in obs.check_results))):
                if tool_input.get("goal_achieved", False):
                    print("  [THINKING] 硬件有失败信号但 AI 判目标达成，可能误判")
                    return True
        return False

    # ── 自动重试（续思） ─────────────────────────────────────────

    def _build_retry_prompt(self, original_content: str) -> str:
        """构建续思提示"""
        return (
            f"{original_content}\n\n"
            f"---\n"
            f"[SYSTEM] 注意：你的上一轮思考被 token 预算截断了，"
            f"导致推理未能完整完成。请忽略之前的输出，"
            f"重新深入分析以上硬件反馈，确保充分推理后再做决策。"
            f"不要急着下结论，仔细检查每个信号。"
        )

    # ── 工具调用提取 ────────────────────────────────────────────

    def _extract_tool_call(self, response) -> tuple:
        """从响应中提取工具调用的 (name, input_dict)"""
        for block in response.content:
            if block.type == "tool_use":
                return block.name, dict(block.input)
        return (None, None)

    # ── 子代理系统 ──────────────────────────────────────────────

    def _run_sub_agents(self, tasks: list[dict],
                        obs: Observation) -> list[dict]:
        """
        并行执行子代理任务。

        每个子代理:
          - 读取对应文件的当前内容
          - 接收主模型写的修改指令
          - 输出修改后的文件内容

        规则执行:
          - 子代理 depth=1（不再能开子代理）
          - 子代理不共享上下文（完全隔离）
          - 子代理只用 output_file 工具
        """
        results = []
        for task in tasks:
            file_rel = task.get("file", "")
            instructions = task.get("instructions", "")
            if not file_rel:
                continue

            # 读取当前文件内容
            file_path = self.project_dir / file_rel
            current_content = ""
            if file_path.exists():
                current_content = file_path.read_text(encoding="utf-8")
            else:
                print(f"  [SUB] 文件不存在，将创建: {file_rel}")

            print(f"  [SUB] 启动子代理: {file_rel}...", end=" ", flush=True)

            # 创建子代理实例（depth=1，不可再委派）
            sub = ClaudeAIBackend(
                api_key=self.api_key,
                model=self.model,
                depth=1,  # 子代理封顶
                project_dir=str(self.project_dir),
            )

            # 构建子代理的输入
            sub_prompt = (
                f"You are editing file: {file_rel}\n\n"
                f"## Instructions from the main engineer\n{instructions}\n\n"
                f"## Hardware feedback context\n"
                f"  Iteration: {obs.iteration}\n"
                f"  Build: {'OK' if obs.build_success else 'FAIL'}\n"
                f"  Assertions: {obs.summary()}\n\n"
                f"## Current file content\n"
                f"```\n{current_content}\n```\n\n"
                f"Output the COMPLETE modified file content "
                f"using the output_file tool."
            )

            try:
                sub_result = sub._execute_sub_agent_task(sub_prompt)
                results.append({
                    "file": file_rel,
                    "content": sub_result.get("content", current_content),
                    "summary": sub_result.get("summary", ""),
                    "success": True,
                })
                print(f"[OK] {sub_result.get('summary', '')[:60]}")
            except Exception as e:
                print(f"[FAIL] {e}")
                results.append({
                    "file": file_rel,
                    "content": current_content,
                    "summary": f"子代理失败: {e}",
                    "success": False,
                })

        return results

    def _execute_sub_agent_task(self, prompt: str) -> dict:
        """
        子代理执行单一文件编辑任务。

        返回: {"content": str, "summary": str}
        """
        # 子代理用 SUB_AGENT_TOOL（只有 output_file）
        sub_tools = [self.SUB_AGENT_TOOL]
        sub_tool_choice = {"type": "any"}

        try:
            kwargs = {
                "model": self.model,
                "max_tokens": self.max_output_tokens,
                "system": self.SUB_AGENT_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
                "tools": sub_tools,
                "tool_choice": sub_tool_choice,
            }
            if self.thinking_config:
                kwargs["thinking"] = self.thinking_config
            kwargs["timeout"] = self.api_timeout

            response = self.client.messages.create(**kwargs)

            for block in response.content:
                if block.type == "tool_use" and block.name == "output_file":
                    return dict(block.input)

            # fallback: 没有 tool_use 则尝试解析文本
            print("  [SUB] 子代理未调用 output_file，尝试文本回退")
            return {"content": response.content[0].text if response.content else "",
                    "summary": "文本回退输出"}

        except Exception as e:
            print(f"  [SUB] 子代理 API 调用失败: {e}")
            return {"content": "", "summary": f"API 错误: {e}"}

    def _finalize_with_sub_results(self, sub_results: list[dict],
                                    obs: Observation,
                                    scenario_desc: str,
                                    memory_context: str = "") -> AIDecision:
        """
        子代理执行完毕后，汇总结果给主模型做最终决策。
        """
        # 构建汇总报告
        summary_lines = ["\n\n## 子代理执行结果汇总\n"]
        for r in sub_results:
            status = "[OK]" if r.get("success") else "[FAIL]"
            summary_lines.append(
                f"  {status} {r['file']}: {r.get('summary', '')[:100]}"
            )
        summary_text = "\n".join(summary_lines)

        # 重新构建用户消息
        feedback_text = obs.to_ai_prompt(
            scenario_desc, memory_context=memory_context, compact=True
        )

        user_content = (
            f"{feedback_text}"
            f"{summary_text}\n\n"
            f"子代理已执行完毕。现在请你做最终决策:\n"
            f"1. 如果目标已达成，调用 submit_decision 并设 goal_achieved=true\n"
            f"2. 如果还需要修改，在 submit_decision 的 code_changes 中"
            f"填入子代理修改后的完整文件内容（从上方结果中获取）\n"
            f"注意: code_changes 中的 content 必须是完整的文件内容。"
        )

        try:
            # 用主模型做最终决策（但不再显示 delegate 工具，防止循环委托）
            self._allow_delegation = False
            response = self._call_api(user_content)
            self._allow_delegation = True  # 恢复

            tool_name, tool_input = self._extract_tool_call(response)

            if tool_name == "submit_decision" and tool_input:
                decision = AIDecision(
                    goal_achieved=tool_input.get("goal_achieved", False),
                    reasoning=tool_input.get("reasoning", ""),
                    code_changes=tool_input.get("code_changes", []),
                )
            else:
                # fallback: 直接使用子代理结果
                decision = AIDecision(
                    goal_achieved=False,
                    reasoning="子代理已完成，请检查修改结果",
                    code_changes=[
                        {"file": r["file"],
                         "description": r.get("summary", "子代理修改"),
                         "content": r["content"]}
                        for r in sub_results if r.get("success")
                    ],
                )

            self._record_history(obs, decision)
            self._print_decision(obs, decision, sub_agent_note=
                                 f"（含 {len(sub_results)} 个子代理任务）")
            return decision

        except Exception as e:
            print(f"\n[AI] [ERROR] 最终决策 API 调用失败: {e}")
            # fallback
            return AIDecision(
                goal_achieved=False,
                reasoning=f"最终决策失败: {e}",
                code_changes=[
                    {"file": r["file"],
                     "description": r.get("summary", ""),
                     "content": r["content"]}
                    for r in sub_results if r.get("success")
                ],
            )

    # ── 历史管理 ────────────────────────────────────────────────

    def _build_history_context(self) -> str:
        if not self.history_summaries:
            return ""
        lines = ["\n\n## 历史迭代记录"]
        for s in self.history_summaries[-5:]:
            lines.append(f"  {s}")
        return "\n".join(lines)

    def _record_history(self, obs: Observation, decision: AIDecision):
        status = "[OK]达成" if decision.goal_achieved else "[RETRY]未达成"
        checks = obs.summary()
        changes = ", ".join(
            c["file"].split("/")[-1] for c in decision.code_changes
        ) if decision.code_changes else "无修改"
        summary = (
            f"第{obs.iteration}轮: "
            f"编译{'OK' if obs.build_success else 'FAIL'} | "
            f"烧录{'OK' if obs.flash_success else 'FAIL'} | "
            f"断言 {checks} | "
            f"AI判定{status} | "
            f"修改: {changes}"
        )
        self.history_summaries.append(summary)
        self.last_iteration = obs.iteration

    # ── 决策打印 ────────────────────────────────────────────────

    def _print_decision(self, obs: Observation, decision: AIDecision,
                        sub_agent_note: str = ""):
        status_text = "[OK]" if decision.goal_achieved else "[RETRY]"
        print(f"\n[{obs.iteration}] {status_text} AI 决策: "
              f"{'目标达成' if decision.goal_achieved else '继续修改'}"
              f"{sub_agent_note}")
        print(f"  |- {decision.reasoning[:300]}")

        if decision.code_changes:
            print(f"  |- 修改 {len(decision.code_changes)} 个文件:")
            for c in decision.code_changes:
                fname = c.get("file", "?")
                desc = c.get("description", "")
                content_len = len(c.get("content", ""))
                print(f"  |   |- {fname}")
                print(f"  |   |   '- {desc} ({content_len} chars)")
        print()

    # ── 工具方法 ────────────────────────────────────────────────

    def reset(self):
        self.history_summaries.clear()
        self.last_iteration = 0

    @staticmethod
    def check_prerequisites() -> list[str]:
        issues = []
        has_key = (
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("HARNESS_API_KEY")
            or os.environ.get("harness_api_key")
        )
        if not has_key:
            issues.append("环境变量 ANTHROPIC_API_KEY / DEEPSEEK_API_KEY / HARNESS_API_KEY 均未设置")
        try:
            import anthropic  # noqa
        except ImportError:
            issues.append("anthropic 包未安装 (pip install anthropic)")
        return issues
