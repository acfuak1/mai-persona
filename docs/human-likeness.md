# 更拟人可以加什么（已按 MaiBot 原生能力去重）

只看三件事是否成立，才值得由插件实现：

1. **需要跨轮次、跨天的持久状态**——MaiBot 的人格提示词是无状态的，每轮都要重新描述。
2. **需要随时间衰减或累计**——例如情绪回落到基线、关系缓慢变化。
3. **需要按用户区分并长期演进**——同一句话对刚认识的人和老朋友应有不同分量。

凡是 MaiBot 人格配置、长期记忆检索、Planner/Replyer 提示词已经能稳定表达的，本文一律**不重复实现**，只在人格配置里写清楚。

## MaiBot 原生就能做，不放进插件

| 能力 | MaiBot 原生做法 | 结论 |
| --- | --- | --- |
| 说话口吻、性格、依恋式表达（黏人/疏离） | `[personality]` 的 `personality` / `behavior_style` / `reply_style`，支持多套 profiles | 不实现，写进人格即可 |
| 是否追问、何时接话、何时安静 | `behavior_style` | 不实现 |
| 用户分享好消息时如何回应（建设性回应） | `behavior_style` + `reply_style` 一句话即可表达 | 不实现 |
| 对话收尾的语气（温和道别） | `reply_style` | 不实现；插件只保留“结束信号 → 排程” |
| 具体事实回指（“你上次说的那份报告”） | 长期记忆检索：`enable_memory_query_tool`、启发式回忆 | 不实现，插件不复制记忆 |
| 角色的偏好、忌讳、长期目标设定 | `personality` 文本 | 不实现；插件只让目标随事件线变化 |
| 打字延迟、分段发送等节奏 | 已有 `local.human-reply-speed` 等插件 | 不实现 |
| 主动发消息这个动作本身 | `maisaka.proactive.trigger` 能力 | 不重复造；插件只决定“何时、找谁、为什么” |

这些能力即使由插件再实现一遍，也只会和人格配置打架，或者让同一个行为有两个来源。

## 值得做：三件需要状态的事（`0.1.9` 已实现）

### 1. 重要披露 → 延迟跟进

**研究**：亲密感取决于“感知到对方理解、认可并关心自己”（Reis & Shaver, 1988）。Laurenceau、Barrett 与 Pietromonaco（1998）发现“自我披露 → 感知回应 → 亲密感”的路径在日常互动中成立；关键在于**事后仍记得并回应**，而不是当轮追问。

**为什么 MaiBot 原生不够**：记忆能检索到“用户说过要面试”，但没有任何机制在 12–48 小时后主动提起；人格提示词也无法保证“这一条还没跟进”。

**实现**：
- 每 `followup_extract_hours` 把近期用户发言交给 Planner，抽取“值得之后关心一次”的事项；每条必须逐字引用原句，否则丢弃。
- 事项写入 `relationship_events(kind='followup')`，带 `due_at`；到期后注入 Planner 与回复上下文，由 Planner 决定是否自然提起。
- 已自然问到可调用 `resolve_persona_event` 结案；超过 `followup_ttl_hours` 自动失效。

**边界**：只跟进用户主动说过的内容；敏感话题不重提；跟进只是提示，不额外占用主动联系次数。

### 2. 关系阶段门控的披露深度

**研究**：自我披露与喜欢互为因果（Collins & Miller, 1994）；逐步加深的披露能显著提升亲近感（Aron et al., 1997）；社会渗透理论强调深度要与关系阶段匹配。

**为什么 MaiBot 原生不够**：人格提示词不知道“对这个人现在处于第几阶段”，只能给一个统一口吻。

**实现**：
- 按关系阶段序号推导披露深度：第 1 阶段表层、第 2 阶段个人经历、第 3 阶段起私密感受，见 `engine_humanlike.disclosure_depth`。
- 每轮回复上下文注入一句对应的披露提示；`[persona].disclosure_depth_gating` 可关闭。

**边界**：披露素材只能来自角色设定与事件线，不能编造与人格冲突的经历；只影响表达倾向，不改变关系阶段。

### 3. 情绪残留与冲突修复

**研究**：情绪会在互动中传染并延续（Hatfield, Cacioppo & Rapson, 1993）；Gottman（1994）的观察研究强调摩擦后的“修复尝试”与正负互动比例对关系稳定的作用。

**为什么 MaiBot 原生不够**：`reply_style` 只能让角色“显得温和”，但无法表达“上一轮被冷落后的低落还没散去”，也不会记录修复是否发生。

**实现**：
- 情绪残留：用户明确表达负面情绪后写入 `carry_over_until`，这段时间内即时情绪的回归时间常数放大 2.5 倍，之后自动恢复。
- 修复提示：检测到指责、冷淡或对抗意味的发言后，写入 `relationship_events(kind='repair')`，在 Planner 与回复上下文中提示“先承认感受再修复”。
- 用户随后给出正面表达时自动结案；超过 `repair_ttl_hours` 自动失效；不改变关系阶段。

**边界**：同步幅度远小于用户；不把不健康互动浪漫化；提供关闭开关。

---

## 参考

- Aron, A., Melinat, E., Aron, E. N., Vallone, R. D., & Bator, R. J. (1997). The experimental generation of interpersonal closeness. *Personality and Social Psychology Bulletin*, 23(4), 363–370.
- Collins, N. L., & Miller, L. C. (1994). Self-disclosure and liking: A meta-analytic review. *Psychological Bulletin*, 116(3), 457–475.
- Gottman, J. M. (1994). *Why Marriages Succeed or Fail*. Simon & Schuster.
- Hatfield, E., Cacioppo, J. T., & Rapson, R. L. (1993). Emotional contagion. *Current Directions in Psychological Science*, 2(3), 96–99.
- Laurenceau, J.-P., Barrett, L. F., & Pietromonaco, P. R. (1998). Intimacy as an interpersonal process. *Journal of Personality and Social Psychology*, 74(5), 1238–1251.
- Reis, H. T., & Shaver, P. (1988). Intimacy as an interpersonal process. In *Handbook of Personal Relationships*.
