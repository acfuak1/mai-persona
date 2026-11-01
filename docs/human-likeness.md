# 更拟人可以加什么：研究依据与落地方案

本文把人格/关系心理学里较稳定的结论，映射成 Mai Persona 可以实际实现的功能。每条都给出：研究结论、可落地功能、涉及的数据与模块、风险与边界。

> 说明：这些是一般人群研究结论，用于设计启发，不代表对具体用户必然成立；插件应始终允许用户关闭或调低相关行为。

## 优先级总览

| 优先级 | 功能 | 依赖现有数据 | 预计工作量 |
| --- | --- | --- | --- |
| P0 | 回应性建模 + 重要披露跟进 | `interaction_queue`、`todo`、日记 | 中 |
| P0 | 建设性回应（好消息放大） | `interaction_queue`、情绪引擎 | 小 |
| P1 | 提问与倾听（未回应问题补偿） | `interaction_queue` | 小 |
| P1 | 自我披露递进与互惠 | `relationship`、`affection` | 中 |
| P1 | 优雅收尾（对话结束检测） | 已有 `is_conversation_end` | 小（扩展） |
| P2 | 依恋倾向 | `relationship`、`affection` | 中 |
| P2 | 情绪传染与冲突修复 | 情绪引擎、`affection_log` | 中 |
| P2 | 节奏拟真（可变延迟） | 回复侧 Hook | 小 |
| P2 | 具体化记忆回指 | 插件互动表 + 宿主记忆 | 中 |
| P3 | 心智感知（稳定的偏好/忌讳/目标） | `persona` 扩展 | 大 |

---

## 1. 回应性建模：让角色"接得住"重要的事

**研究**：亲密感的核心不是聊得多，而是"感知到对方理解、认可并关心自己"（Reis & Shaver, 1988 亲密过程模型）。Laurenceau、Barrett 与 Pietromonaco（1998）发现，自我披露 → 感知回应 → 亲密感的路径在每日互动中成立。

**功能**：
- 给互动队列加一层轻量标注：`disclosure`（用户透露的重要个人信息/情绪/计划）、`bid`（寻求回应的小信号）。
- 对 `disclosure` 建立"跟进事项"：在 12–48 小时内，于合适时机自然提一句（"面试结果怎么样了？"），而不是当轮追问。
- 跟进成功/失败写入关系日志，作为"回应性"证据之一。

**实现**：复用 `todo` 表增加 `kind='followup'`；Planner 任务新增 `followup_generate`；由 `get_persona_pending_work` 下发。

**边界**：只跟进用户主动说过的内容；隐私话题不主动重提；跟进频率受每日主动上限约束。

## 2. 建设性回应：好消息要"放大"而不是"收到"

**研究**：Gable、Reis、Impett 与 Asher（2004）发现，对伴侣好消息做出"主动—建设性"回应（热情 + 追问细节）比被动回应更能提升关系满意度，这一效应独立于坏消息支持。

**功能**：
- 在 Replyer 注入的上下文里标记"本轮是否包含好消息/成就"。
- 给角色一个表达提示：先共情情绪，再追问一个具体细节；避免"恭喜"式一句话结束。
- 记录用户分享的好消息，作为后续回指素材。

**实现**：`interaction_appraisal` 增加 `positive_event` 字段；`_state_modifier` 增加一句表达提示。

**边界**：不夸大、不替用户编造细节；用户不想展开时不追问。

## 3. 自我披露递进与互惠

**研究**：Collins 与 Miller（1994）的元分析显示自我披露与喜欢互为因果；Aron 等（1997）的"逐级深入问题"实验表明，逐步加深的披露能在短时间内显著提升亲近感。社会渗透理论同样强调深度要匹配关系阶段。

**功能**：
- 按 `relationship_stages` 设定"可披露深度"：陌生阶段只聊表层，熟悉后再出现个人经历。
- 互惠：用户披露后，角色以**同等深度**回一段自己的事（而不是继续追问）。
- 用 `affection.intimacy` 控制"愿意说的比例"，避免刚认识就掏心掏肺。

**实现**：`persona` 增加每阶段 `disclosure_depth` 映射；`_state_modifier` 按阶段注入一句披露策略。

**边界**：角色自有设定（`goals`、事件线）是披露素材来源，不能凭空编造与人格冲突的经历。

## 4. 提问与倾听：补上没被回应的对话

**研究**：Huang、Yeomans、Brooks、Minson 与 Gino（2017）发现，多提问（尤其是追问）能显著提升对方的好感；提问者常低估这一点。

**功能**：
- 统计每个会话的"提问—回应"配对，识别用户提出但角色没接住的问题。
- 在后续轮次自然补一句回应，或在 Planner 维护任务中提示"上次有一个问题没有回答"。

**实现**：`interaction_queue` 增加 `question` 标记；新增每日任务 `unanswered_question`。

**边界**：只补偿真正的问题，不做机械式追问清单。

## 5. 优雅收尾：知道什么时候该停

**研究**：Mastroianni 等（2021, PNAS）发现对话双方常常都想更早结束，却都以为对方还想聊；主动、温和地收尾反而提升体验。

**功能**：
- 在现有 `is_conversation_end` 基础上，增加"话题已收束/对方回复变短/连续多轮无新信息"的弱信号。
- 收尾时给出一句自然的结束语（不硬转话题），并把"下次再聊"作为排程决策的输入。

**实现**：扩展 `engine_schedule.is_conversation_end` 为分级信号；与私聊结束排程打通。

**边界**：不在用户情绪低落或正在倾诉时收尾。

## 6. 依恋倾向：让"靠近"和"退开"有性格

**研究**：Hazan 与 Shaver（1987）把成人依恋分为安全型、焦虑型、回避型；不同倾向影响寻求安慰、表达需求和对亲密的舒适度。

**功能**：
- 配置角色依恋倾向（安全/焦虑/回避，或 0–1 的两个维度）。
- 焦虑倾向：未获回应时更易反复确认；回避倾向：亲密升温后短暂后退。
- 这些只影响"表达方式与主动性"，不改变关系事实。

**实现**：`persona` 新增 `attachment_style`；`_state_modifier` 与排程候选评分使用。

**边界**：避免把不健康模式浪漫化；提供强度上限与关闭开关。

## 7. 情绪传染与冲突修复

**研究**：情绪传染在亲密关系中普遍存在；Gottman 的观察研究强调"修复尝试"与正负互动比例（约 5:1）对关系稳定的作用（Gottman, 1994）。

**功能**：
- 小幅同步用户情绪（已有共情脉冲，可增加"持续性"：低落会延续一到两轮）。
- 检测摩擦（否定、指责、冷处理）后，主动给一次修复尝试，并记录到关系日志。

**实现**：情绪引擎增加 `carry_over` 参数；`affection_log` 增加 `repair` 类型。

**边界**：同步幅度远小于用户；不因一次摩擦下调关系阶段。

## 8. 节奏拟真：延迟也是表达

**研究**：Gnewuch 等（2018）发现"动态响应延迟"（按消息长度/复杂度变化）比固定延迟或零延迟更让人觉得像人。

**功能**：
- 按回复长度、情绪强度和当前活动估算 1–8 秒延迟；忙碌/睡眠时段更长。
- 与"正在输入"状态配合，避免机械固定值。

**实现**：Replyer 侧 Hook 或发送前等待；参数放入 `[plugin]`。

**边界**：延迟上限可配；紧急/明确请求时缩短。

## 9. 具体化记忆：回指细节而不是"我记得你说过"

**研究**：自我披露本身具有奖赏性（Tamir & Mitchell, 2012）；而被准确记住的具体细节，是"被理解"的直接证据（呼应 Reis & Shaver 的回应性）。

**功能**：
- 从互动中抽取"可回指细节"：日期、人物、地点、偏好、承诺。
- 在合适时机具体回指（"你上次说周四要交的那份报告"），而不是泛泛提及。
- 与宿主长期记忆互补：插件保存"具体片段"，宿主管语义检索。

**实现**：`interaction_queue` 增加 `detail` 结构化字段；Planner 任务 `detail_extract`。

**边界**：不确定的细节要问而不是断言；避免复述敏感信息。

## 10. 心智感知：稳定偏好、忌讳与目标

**研究**：Gray、Gray 与 Wegner（2007）区分"体验"（感受）与"主体性"（思考、计划）；Epley、Waytz 与 Cacioppo（2007）指出可预测的动机与知识会增强拟人化感知。Nass 与 Moon（2000）也发现人会把社会规范套用到计算机上。

**功能**：
- 角色拥有稳定的偏好清单（喜欢/讨厌）、忌讳话题、当前目标；
- 目标会随事件线推进而变化，并在对话中偶尔体现（不是每轮都说）。
- 允许角色说"不"：对不喜欢的提议表达轻微抗拒。

**实现**：`persona` 增加 `preferences`、`boundaries`、`active_goals`；事件线引擎维护目标状态。

**边界**：拒绝要温和且可协商，不演变成对抗。

---

## 参考

- Aron, A., Melinat, E., Aron, E. N., Vallone, R. D., & Bator, R. J. (1997). The experimental generation of interpersonal closeness. *Personality and Social Psychology Bulletin*, 23(4), 363–370.
- Collins, N. L., & Miller, L. C. (1994). Self-disclosure and liking: A meta-analytic review. *Psychological Bulletin*, 116(3), 457–475.
- Epley, N., Waytz, A., & Cacioppo, J. T. (2007). On seeing human: A three-factor theory of anthropomorphism. *Psychological Review*, 114(4), 864–886.
- Gable, S. L., Reis, H. T., Impett, E. A., & Asher, E. R. (2004). What do you do when things go right? *Journal of Personality and Social Psychology*, 87(2), 228–245.
- Gnewuch, U., Morana, S., Adam, M. T. P., & Maedche, A. (2018). Faster is not always better: Response-time effects on user perceptions of chatbots. *ECIS 2018*.
- Gottman, J. M. (1994). *Why Marriages Succeed or Fail*. Simon & Schuster.
- Gray, H. M., Gray, K., & Wegner, D. M. (2007). Dimensions of mind perception. *Science*, 315(5812), 619.
- Hazan, C., & Shaver, P. (1987). Romantic love conceptualized as an attachment process. *Journal of Personality and Social Psychology*, 52(3), 511–524.
- Huang, K., Yeomans, M., Brooks, A. W., Minson, J., & Gino, F. (2017). It doesn't hurt to ask: Question-asking increases liking. *Journal of Personality and Social Psychology*, 113(3), 430–445.
- Laurenceau, J.-P., Barrett, L. F., & Pietromonaco, P. R. (1998). Intimacy as an interpersonal process. *Journal of Personality and Social Psychology*, 74(5), 1238–1251.
- Mastroianni, A. M., Gilbert, D. T., et al. (2021). Do conversations end when people want them to? *PNAS*, 118(10).
- Nass, C., & Moon, Y. (2000). Machines and mindlessness: Social responses to computers. *Journal of Social Issues*, 56(1), 81–103.
- Reis, H. T., & Shaver, P. (1988). Intimacy as an interpersonal process. In *Handbook of Personal Relationships*.
- Tamir, D. I., & Mitchell, J. P. (2012). Disclosing information about the self is intrinsically rewarding. *PNAS*, 109(21), 8038–8043.
