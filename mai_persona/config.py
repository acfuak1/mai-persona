"""Mai Persona 配置模型。"""

import re
from typing import Any

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator, model_validator


def _ui_field(label: str, description: str, **kwargs: Any) -> Any:
    """给 MaiBot 配置表单提供中文字段名，同时保留 Pydantic 校验参数。"""

    extra = dict(kwargs.pop("json_schema_extra", {}) or {})
    extra.setdefault("label", label)
    for key in (
        "placeholder",
        "hint",
        "hidden",
        "disabled",
        "order",
        "input_type",
        "x-widget",
        "x-icon",
        "rows",
        "group",
        "depends_on",
        "depends_value",
        "min_items",
        "max_items",
        "example",
        "step",
        "pattern",
        "max_length",
    ):
        if key in kwargs:
            extra.setdefault(key, kwargs.pop(key))
    hint = str(extra.get("hint", "")).strip()
    extra["hint"] = " ".join(part for part in (description.strip(), hint) if part)
    return Field(description=description, json_schema_extra=extra, **kwargs)


def _validate_hhmm(value: str) -> str:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value or ""))
    if not match:
        raise ValueError("时间必须使用 HH:MM 格式")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("时间必须是有效的 24 小时时间")
    return f"{hour:02d}:{minute:02d}"


class PluginOptions(PluginConfigBase):
    """时区、后台检查及重试设置；插件启停由设置页顶部的开关控制。"""

    __ui_label__ = "运行设置"
    __ui_icon__ = "settings"
    __ui_order__ = 1

    enabled: bool = _ui_field(
        "启用 Mai Persona",
        "打开后开始记录和维护角色状态。角色资料尚未填写时也可以启用，补齐后会自动启动。",
        default=False,
        json_schema_extra={"hidden": True},
    )
    config_version: str = _ui_field(
        "配置版本",
        "由 MaiBot 自动维护，请勿手动修改。",
        default="1.0.0",
        json_schema_extra={"hidden": True, "order": 99},
    )
    timezone: str = _ui_field(
        "所在时区",
        "用于日程、每日任务和记录时间。",
        default="Asia/Shanghai",
        placeholder="Asia/Shanghai",
        hint="通常保持默认值即可。",
        order=0,
    )
    enable_proactive_messages: bool = _ui_field(
        "允许主动联系",
        "默认关闭。开启后，插件会按排程请求 MaiBot Planner 发起一轮主动对话；是否回复仍由 Planner 判断。",
        default=False,
        hint="建议先完成普通对话测试，再单独开启此项。",
        order=1,
    )
    tick_seconds: int = _ui_field(
        "后台检查间隔（秒）",
        "插件检查状态衰减和到期维护任务的频率；提示词任务只会排入队列，由后续 Planner 轮次处理。",
        default=60,
        ge=10,
        le=3600,
        step=10,
        order=2,
    )
    max_interaction_chars: int = _ui_field(
        "单条对话记录长度上限",
        "插件本地互动记录的单条字符上限，超出部分会截断；不会写入 MaiBot 长期记忆。",
        default=1200,
        ge=100,
        le=10000,
        step=100,
        order=3,
    )
    retry_cooldown_minutes: float = _ui_field(
        "失败重试间隔（分钟）",
        "维护任务或主动排程失败后，至少等待多久再重试。",
        default=15.0,
        ge=1.0,
        le=1440.0,
        step=1,
        order=4,
    )


class EmotionConfig(PluginConfigBase):
    """情绪基线、衰减和评价约束。"""

    __ui_label__ = "情绪与心境"
    __ui_icon__ = "mood"
    __ui_order__ = 2

    trait_p: float = _ui_field(
        "愉悦度基线（P）",
        "角色稳定的愉悦维度基线，范围 -1 至 1；0 表示中性。",
        default=0.0,
        ge=-1.0,
        le=1.0,
        step=0.05,
        group="角色基线",
    )
    trait_a: float = _ui_field(
        "活跃度基线（A）",
        "角色稳定的活跃维度基线，范围 -1 至 1；0 表示中性。",
        default=0.0,
        ge=-1.0,
        le=1.0,
        step=0.05,
        group="角色基线",
    )
    trait_d: float = _ui_field(
        "主导度基线（D）",
        "角色稳定的主导维度基线，范围 -1 至 1；0 表示中性。",
        default=0.0,
        ge=-1.0,
        le=1.0,
        step=0.05,
        group="角色基线",
    )
    tau_emotion_min: float = _ui_field(
        "即时情绪回归时间（分钟）",
        "即时情绪向当前心境回归的时间尺度；数值越小，回归越快。",
        default=20.0,
        gt=0.0,
        le=10080.0,
        step=1,
        group="状态回归",
    )
    tau_mood_hours: float = _ui_field(
        "心境回归时间（小时）",
        "心境向角色气质基线回归的时间尺度；数值越小，回归越快。",
        default=12.0,
        gt=0.0,
        le=8760.0,
        step=1,
        group="状态回归",
    )
    regulation: float = _ui_field(
        "情绪表达收敛",
        "对外表达时收敛即时情绪偏移的比例。",
        default=0.65,
        ge=0.0,
        le=1.0,
        step=0.05,
        group="状态回归",
    )
    appraisal_window_hours: float = _ui_field(
        "对话评价间隔（小时）",
        "两次批量评价近期对话之间的最短间隔；任务由 Planner 处理。",
        default=2.0,
        gt=0.0,
        le=168.0,
        step=0.5,
        group="评价限制",
    )
    max_delta_per_event: float = _ui_field(
        "单个事件影响上限",
        "限制 Planner 对单个事件提交的 PAD 变化幅度，范围 0 至 1。",
        default=0.3,
        ge=0.0,
        le=1.0,
        step=0.05,
        group="评价限制",
    )
    max_micro_event_bias: float = _ui_field(
        "日常事件情绪偏移上限",
        "限制日常安排对候选情绪的影响。",
        default=0.15,
        ge=0.0,
        le=1.0,
        step=0.05,
        group="评价限制",
    )


class AffectionConfig(PluginConfigBase):
    """好感评价约束。"""

    __ui_label__ = "关系与好感"
    __ui_icon__ = "favorite"
    __ui_order__ = 3

    evaluate_daily_at: str = _ui_field(
        "每日评估时间",
        "到点后生成关系与好感维护任务，由后续 Planner 轮次处理。",
        default="23:00",
        placeholder="23:00",
        hint="使用 24 小时制，例如 23:00。",
        group="关系评估",
    )
    max_delta: float = _ui_field(
        "单次变化上限",
        "限制信任、亲密和默契的单次变化幅度，范围 0 至 1。",
        default=0.1,
        ge=0.0,
        le=1.0,
        step=0.01,
        group="关系评估",
    )
    evidence_hours: float = _ui_field(
        "事实回溯时长（小时）",
        "只依据此时间范围内检索到的聊天事实评估；没有明确事实时不改变数值。",
        default=24.0,
        gt=0.0,
        le=720.0,
        step=1,
        group="关系评估",
    )

    @field_validator("evaluate_daily_at")
    @classmethod
    def validate_evaluate_daily_at(cls, value: str) -> str:
        return _validate_hhmm(value)


class EventLinesConfig(PluginConfigBase):
    """跨日事件线设置。"""

    __ui_label__ = "长期事件线"
    __ui_icon__ = "timeline"
    __ui_order__ = 4

    max_active: int = _ui_field(
        "同时进行的事件线",
        "限制同时保留的活跃事件线数量；不会为了填满数量而创建事件。",
        default=3,
        ge=1,
        le=20,
        group="推进节奏",
    )
    advance_times: list[str] = _ui_field(
        "每日推进时刻",
        "到点后排入 Planner 维护队列，等待下一次 Planner 轮次处理。",
        default_factory=lambda: ["13:00", "21:00"],
        example=["13:00", "21:00"],
        placeholder="21:00",
        hint="按 24 小时制填写，每项一个时间，例如 13:00、21:00。",
        group="推进节奏",
    )
    talkable_threshold: float = _ui_field(
        "主动提及阈值",
        "事件线达到此倾向值后才会进入回复上下文；数值越高，越不容易被提及。",
        default=0.6,
        ge=0.0,
        le=1.0,
        step=0.05,
        group="推进节奏",
    )

    @field_validator("advance_times")
    @classmethod
    def validate_advance_times(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(_validate_hhmm(value) for value in values))


class ScheduleConfig(PluginConfigBase):
    """主动排程和发送护栏。"""

    __ui_label__ = "主动联系规则"
    __ui_icon__ = "schedule"
    __ui_order__ = 5

    min_interval_hours: float = _ui_field(
        "主动联系最短间隔（小时）",
        "两次主动联系至少间隔多久。",
        default=4.0,
        ge=0.0,
        le=720.0,
        step=0.5,
        group="发送护栏",
    )
    daily_limit: int = _ui_field(
        "每日主动联系上限",
        "每天允许排入 Planner 的主动联系轮数；0 表示禁止主动联系。",
        default=3,
        ge=0,
        le=100,
        group="发送护栏",
    )
    silent_hours: list[int] = _ui_field(
        "静默时段（开始、结束小时）",
        "填写两个 0 至 23 的小时值，例如 [1, 7] 表示每天 01:00 至 07:00 不主动联系。",
        default_factory=lambda: [1, 7],
        example=[1, 7],
        hint="这里是小时数，不填写分钟。",
        group="发送护栏",
    )
    default_active_hour_hint: int = _ui_field(
        "默认建议联系小时",
        "缺少用户活跃时段资料时，供 Planner 参考的小时数。",
        default=21,
        ge=0,
        le=23,
        group="Planner 排程",
    )
    fallback_replan_hours: float = _ui_field(
        "重新评估排程间隔（小时）",
        "当前没有等待发送的排程时，间隔多久再次请 Planner 评估。",
        default=24.0,
        gt=0.0,
        le=720.0,
        step=1,
        group="Planner 排程",
    )

    @field_validator("silent_hours")
    @classmethod
    def validate_silent_hours(cls, values: list[int]) -> list[int]:
        if len(values) != 2 or any(value < 0 or value > 23 for value in values):
            raise ValueError("静默时段必须包含两个 0 至 23 的小时值")
        if values[0] == values[1]:
            raise ValueError("静默时段起止小时不能相同")
        return values


class BatchConfig(PluginConfigBase):
    """周期性批量任务设置。"""

    __ui_label__ = "日常维护任务"
    __ui_icon__ = "event_repeat"
    __ui_order__ = 6

    todo_extract_hours: float = _ui_field(
        "待办整理间隔（小时）",
        "到期后收集待办候选并加入 Planner 队列；是否建立或更新待办由 Planner 判断。",
        default=4.0,
        gt=0.0,
        le=168.0,
        step=0.5,
        group="待办整理",
    )
    life_generate_at: str = _ui_field(
        "日常安排生成时刻",
        "到点后加入 Planner 维护队列，等待下一次 Planner 轮次生成。启用 MaiTrace 日程后，此项只触发日常小事件生成。",
        default="08:00",
        placeholder="08:00",
        hint="使用 24 小时制，例如 08:00。",
        group="日程与日记",
    )
    use_maitrace_schedule: bool = _ui_field(
        "使用 MaiTrace 日程",
        "开启后只读接入 MaiTrace 日程 API，并将当前活动和后续活动提供给 Planner、Replyer。Mai Persona 不再生成自己的日程，但仍会生成日常小事件。",
        default=False,
        hint="需要启用 MaiTrace 的日程功能。接口暂不可用时会明确显示不可用，不会用本地旧日程替代；不会向 MaiTrace 写入数据。",
        group="日程与日记",
    )
    diary_generate_at: str = _ui_field(
        "日记整理时刻",
        "到点后加入 Planner 维护队列，等待下一次 Planner 轮次整理。",
        default="23:30",
        placeholder="23:30",
        hint="日记只保存在插件自己的数据库中，不写入 MaiBot 长期记忆。",
        group="日程与日记",
    )

    @field_validator("life_generate_at", "diary_generate_at")
    @classmethod
    def validate_daily_time(cls, value: str) -> str:
        return _validate_hhmm(value)


class ExposureConfig(PluginConfigBase):
    """角色内在状态的低频显露限制。"""

    __ui_label__ = "状态查询限制"
    __ui_icon__ = "visibility"
    __ui_order__ = 7

    daily_limit: int = _ui_field(
        "每日状态查询上限",
        "限制 Planner 通过 get_inner_state 工具查询完整内在状态的次数；0 表示禁止查询。",
        default=2,
        ge=0,
        le=20,
    )


class PersonaConfig(PluginConfigBase):
    """供内在状态与生活模拟使用的补充资料。"""

    __ui_label__ = "角色设定"
    __ui_icon__ = "person"
    __ui_order__ = 0

    name: str = _ui_field(
        "角色名称",
        "填写与 MaiBot 自有人格设置一致的角色称呼；此处仅用于标记插件维护的内在状态。",
        default="",
        placeholder="例如：小麦",
        hint="此项留空时仍可保存并启用插件，补齐后角色引擎才会启动。",
        group="状态与生活模拟",
    )
    viewpoint: list[str] = _ui_field(
        "状态评价视角",
        "只填写用于判断情绪、关系和生活事件的观察原则。不要在这里重复设置总体人格、行为准则或说话风格；这些由 MaiBot 自有人格设置负责。",
        default_factory=list,
        example=["先区分事实与猜测，再评价情绪", "单次未回复不直接视为关系变差"],
        placeholder="例如：先理解事实，再判断情绪",
        hint="每项单独填写；建议写具体的状态判断依据，不要写回复口吻或对用户的行为要求。",
        group="状态与生活模拟",
    )
    goals: str = _ui_field(
        "长期生活方向",
        "描述角色个人生活中持续在意的方向，仅用于生活事件、活动和内在状态模拟；不要写成对话任务或必须向用户完成的目标。",
        default="",
        placeholder="例如：持续学习新的知识，并保持稳定的生活节奏。",
        rows=5,
        json_schema_extra={"x-widget": "textarea"},
        hint="这不是 MaiBot 的总体人格目标，也不要求角色在每轮对话中提及或推进。",
        group="状态与生活模拟",
    )
    relationship_stages: list[str] = _ui_field(
        "关系阶段",
        "按关系由浅到深排列。首次互动从第一阶段开始，之后每次只允许向相邻阶段变化。",
        default_factory=list,
        example=["陌生", "熟悉", "朋友", "亲密"],
        placeholder="例如：熟悉",
        hint="至少填写两个不重复的阶段，每项单独填写。例如：陌生、熟悉、朋友、亲密。",
        group="状态与生活模拟",
    )
    event_line_domains: list[str] = _ui_field(
        "长期事件线领域",
        "限定角色可以持续发展的生活方向。插件会把具体事件交给 Planner 按角色目标生成。",
        default_factory=list,
        example=["工作与学习", "兴趣爱好"],
        placeholder="例如：工作与学习",
        hint="填写领域即可，不需要预先编写剧情。例如：工作与学习、兴趣爱好。",
        group="状态与生活模拟",
    )
    micro_event_domains: list[str] = _ui_field(
        "日常小事领域",
        "限定日常安排和小事件的题材范围，避免生成超出角色设定的内容。",
        default_factory=list,
        example=["休息与饮食", "阅读和散步"],
        placeholder="例如：休息与饮食",
        hint="每项填写一个方向。例如：休息与饮食、阅读和散步。",
        group="状态与生活模拟",
    )

    @field_validator("relationship_stages")
    @classmethod
    def validate_relationship_stages(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values if value.strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError("关系阶段不能重复")
        return normalized


class MaiPersonaConfig(PluginConfigBase):
    """Mai Persona 插件配置。运行维护任务统一交由 MaiBot Planner 处理。"""

    plugin: PluginOptions = Field(default_factory=PluginOptions)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    emotion: EmotionConfig = Field(default_factory=EmotionConfig)
    affection: AffectionConfig = Field(default_factory=AffectionConfig)
    event_lines: EventLinesConfig = Field(default_factory=EventLinesConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    batch: BatchConfig = Field(default_factory=BatchConfig)
    exposure: ExposureConfig = Field(default_factory=ExposureConfig)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_nested_settings(cls, value: Any) -> Any:
        """把旧版 [persona.*] 选项迁移到独立设置分组，并丢弃旧 API/模型字段。"""

        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        persona = normalized.get("persona")
        if isinstance(persona, dict):
            persona = dict(persona)
            for section in ("emotion", "affection", "event_lines", "schedule", "batch", "exposure"):
                legacy_section = persona.pop(section, None)
                if isinstance(legacy_section, dict):
                    normalized[section] = legacy_section
            persona.pop("budget", None)
            normalized["persona"] = persona

        plugin = normalized.get("plugin")
        if isinstance(plugin, dict):
            plugin = dict(plugin)
            for field_name in (
                "memory_plugin_id",
                "memory_search_api",
                "memory_profile_api",
                "llm_task_name",
                "llm_temperature",
                "llm_max_tokens",
            ):
                plugin.pop(field_name, None)
            normalized["plugin"] = plugin
        return normalized

    def validate_persona(self) -> list[str]:
        """返回启动所需但尚未填写的角色配置项。"""

        missing: list[str] = []
        if not self.persona.name.strip():
            missing.append("角色名称")
        if not any(item.strip() for item in self.persona.viewpoint):
            missing.append("状态评价视角")
        if not self.persona.goals.strip():
            missing.append("长期生活方向")
        if len([item for item in self.persona.relationship_stages if item.strip()]) < 2:
            missing.append("至少两个关系阶段")
        if not any(item.strip() for item in self.persona.event_line_domains):
            missing.append("事件线领域约束")
        if not any(item.strip() for item in self.persona.micro_event_domains):
            missing.append("小事件领域约束")
        return missing
