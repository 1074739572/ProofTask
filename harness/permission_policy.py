"""静态权限策略与风险分级模型（纯解析与分级模块）。

本模块把仓库内静态默认策略 ``config/permissions.json`` 解析并校验为
:class:`PermissionPolicy`，并对外提供两个纯函数接口：

- ``load_permission_policy(path=None)``：加载并校验权限策略文件，返回
  ``PermissionPolicy`` 实例；配置缺失字段、非法 mode 值或 JSON 无法解析时
  抛出 :class:`PermissionPolicyConfigError`（``ValueError`` 子类），不允许
  静默产生未定义权限策略。
- ``classify_tool(tool_name, tool_input)``：把一次工具调用分级为
  ``low`` / ``medium`` / ``high`` / ``blocked`` 之一。bash 按命令内容联合
  分级，其余工具按工具名匹配静态风险表；未识别工具保守返回 ``high``。

本任务只实现静态策略与处理逻辑：不读取、不修改运行时会话模式，也绝不把
任何内容写回 ``config/permissions.json`` 或 ``config/modes.json``。
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Mapping, Optional, Sequence, Tuple

RISK_LEVELS: Tuple[str, ...] = ("low", "medium", "high", "blocked")
"""所有合法风险等级；``blocked`` 为硬阻断，任何模式都不可自动放行。"""

AUTO_APPROVE_LEVELS: Tuple[str, ...] = ("low", "medium", "high")
"""模式自动放行只允许出现这些等级（``blocked`` 被排除在静态校验之外）。"""

UNKNOWN_TOOL_RISK = "high"
"""未识别工具的保守风险等级。"""

DEFAULT_BASH_RISK = "high"
"""bash 无任何模式命中时的默认风险。"""

_SHIPPED_CONFIG_RELPATH = Path("config") / "permissions.json"


class PermissionPolicyConfigError(ValueError):
    """权限静态配置缺失、非法或不可解析时的明确配置错误。"""


@dataclass(frozen=True)
class _ToolRule:
    """一条工具名风险规则；glob 模式按长度取最具体者优先。"""

    pattern: str
    risk: str
    exact: bool
    sort_key: Tuple[int, int]

    def matches(self, tool_name: str) -> bool:
        if self.exact:
            return self.pattern == tool_name
        return fnmatch.fnmatchcase(tool_name, self.pattern)


class PermissionPolicy:
    """一份已加载并校验的静态权限策略（只读对象）。"""

    def __init__(
        self,
        modes: Mapping[str, FrozenSet[str]],
        tool_rules: Sequence[_ToolRule],
        bash_blocked: Sequence[Tuple[re.Pattern, str]],
        bash_high: Sequence[Tuple[re.Pattern, str]],
        bash_low: Sequence[Tuple[re.Pattern, str]],
        bash_default_risk: str,
        source: Path,
    ) -> None:
        self._modes: Dict[str, FrozenSet[str]] = dict(modes)
        self._tool_rules: Tuple[_ToolRule, ...] = tuple(tool_rules)
        self._bash_blocked: Tuple[Tuple[re.Pattern, str], ...] = tuple(bash_blocked)
        self._bash_high: Tuple[Tuple[re.Pattern, str], ...] = tuple(bash_high)
        self._bash_low: Tuple[Tuple[re.Pattern, str], ...] = tuple(bash_low)
        self._bash_default_risk: str = bash_default_risk
        self.source: Path = Path(source)

    # -- 模式查询 --

    @property
    def modes(self) -> Mapping[str, FrozenSet[str]]:
        """模式名 → 该模式自动放行风险等级集合（只读视图）。"""
        return dict(self._modes)

    def auto_approve_levels(self, mode: str) -> FrozenSet[str]:
        """返回该模式自动放行的风险等级集合；未知模式抛明确配置错误。"""
        try:
            return self._modes[mode]
        except KeyError:
            raise PermissionPolicyConfigError(
                f"未知权限模式 {mode!r}；可用模式：{sorted(self._modes)}"
            ) from None

    # -- 工具分类 --

    def classify_tool_name(self, tool_name: str) -> Optional[str]:
        """按工具名匹配静态风险表；未识别返回 None（由调用方保守处理）。"""
        matches = [rule for rule in self._tool_rules if rule.matches(tool_name)]
        if not matches:
            return None
        matches.sort(key=lambda rule: rule.sort_key)
        return matches[0].risk

    def classify_bash(self, command: str) -> str:
        """按命令内容联合分级：blocked → high → low → 默认风险。"""
        command = command or ""
        for pattern, risk in self._bash_blocked:
            if pattern.search(command):
                return risk
        for pattern, risk in self._bash_high:
            if pattern.search(command):
                return risk
        for pattern, risk in self._bash_low:
            if pattern.search(command):
                return risk
        return self._bash_default_risk


def _validate_level(level: Any, context: str) -> str:
    if not isinstance(level, str) or level not in RISK_LEVELS:
        raise PermissionPolicyConfigError(
            f"{context} 含非法风险等级 {level!r}；仅允许 {list(RISK_LEVELS)}"
        )
    return level


def _validate_modes(modes: Any) -> Dict[str, FrozenSet[str]]:
    if not isinstance(modes, dict) or not modes:
        raise PermissionPolicyConfigError("permissions.json 缺少非空 'modes' 字段")
    validated: Dict[str, FrozenSet[str]] = {}
    for name, cfg in modes.items():
        if not isinstance(name, str) or not name:
            raise PermissionPolicyConfigError("'modes' 键必须是非空字符串")
        if not isinstance(cfg, dict):
            raise PermissionPolicyConfigError(f"mode {name!r} 的配置必须是对象")
        auto_approve = cfg.get("auto_approve")
        if auto_approve is None:
            raise PermissionPolicyConfigError(
                f"mode {name!r} 缺少 'auto_approve' 字段"
            )
        if not isinstance(auto_approve, list) or not auto_approve:
            raise PermissionPolicyConfigError(
                f"mode {name!r} 的 'auto_approve' 必须是非空数组"
            )
        levels: FrozenSet[str] = frozenset(
            _validate_level(level, f"mode {name!r} 的 'auto_approve'") for level in auto_approve
        )
        illegal = levels & {"blocked"}
        if illegal:
            raise PermissionPolicyConfigError(
                f"mode {name!r} 的 'auto_approve' 含硬阻断等级 {sorted(illegal)}；"
                "blocked 永不可被任何模式自动放行"
            )
        validated[name] = levels
    return validated


def _compile_bash_patterns(raw: Any, group: str, risk: str) -> Tuple[Tuple[re.Pattern, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise PermissionPolicyConfigError(f"bash.patterns.{group} 必须是数组")
    compiled: list = []
    for item in raw:
        if not isinstance(item, str) or not item:
            raise PermissionPolicyConfigError(
                f"bash.patterns.{group} 的每一项必须是非空字符串"
            )
        try:
            compiled.append((re.compile(item), risk))
        except re.error as exc:
            raise PermissionPolicyConfigError(
                f"bash.patterns.{group} 含非法正则 {item!r}: {exc}"
            ) from None
    return tuple(compiled)


def _load_json_config(path: Path, raw: str) -> Any:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PermissionPolicyConfigError(
            f"无法解析权限配置文件 {path}: {exc}"
        ) from None
    if not isinstance(payload, dict):
        raise PermissionPolicyConfigError(
            f"权限配置文件 {path} 顶层必须是 JSON 对象"
        )
    return payload


def _resolve_config_path(path: Optional[Any]) -> Path:
    if path is None:
        return Path(__file__).resolve().parent.parent / _SHIPPED_CONFIG_RELPATH
    return Path(path)


def load_permission_policy(path: Optional[Any] = None) -> PermissionPolicy:
    """加载并校验静态权限策略，返回只读的 :class:`PermissionPolicy`。

    :param path: 策略文件路径；为 None 时使用仓库内默认配置
        ``config/permissions.json``。校验失败时抛出
        :class:`PermissionPolicyConfigError`（``ValueError`` 子类）。
    """
    config_path = _resolve_config_path(path)
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PermissionPolicyConfigError(
            f"无法读取权限配置文件 {config_path}: {exc}"
        ) from None
    payload = _load_json_config(config_path, raw)

    modes = _validate_modes(payload.get("modes"))

    tools_raw = payload.get("tools")
    if tools_raw is None:
        raise PermissionPolicyConfigError("permissions.json 缺少 'tools' 字段")
    if not isinstance(tools_raw, dict):
        raise PermissionPolicyConfigError("'tools' 必须是对象（工具名 → 风险等级）")
    tool_rules: list = []
    for pattern, risk in tools_raw.items():
        if not isinstance(pattern, str) or not pattern:
            raise PermissionPolicyConfigError("'tools' 的键必须是非空字符串")
        risk = _validate_level(risk, f"工具 {pattern!r}")
        exact = not any(ch in pattern for ch in "*?[")
        length = len(pattern)
        sort_key = (0 if exact else 1, -length)
        tool_rules.append(_ToolRule(pattern=pattern, risk=risk, exact=exact, sort_key=sort_key))

    bash_raw = payload.get("bash")
    if bash_raw is None:
        raise PermissionPolicyConfigError("permissions.json 缺少 'bash' 字段")
    if not isinstance(bash_raw, dict):
        raise PermissionPolicyConfigError("'bash' 必须是对象")
    default_risk = bash_raw.get("default_risk", DEFAULT_BASH_RISK)
    default_risk = _validate_level(default_risk, "bash.default_risk")
    if default_risk == "blocked":
        raise PermissionPolicyConfigError(
            "bash.default_risk 不能是 'blocked'；默认风险只允许 low/medium/high"
        )
    patterns_raw = bash_raw.get("patterns")
    if patterns_raw is None:
        raise PermissionPolicyConfigError("'bash' 缺少 'patterns' 字段")
    if not isinstance(patterns_raw, dict):
        raise PermissionPolicyConfigError("bash.patterns 必须是对象")
    bash_blocked = _compile_bash_patterns(
        patterns_raw.get("blocked"), "blocked", "blocked"
    )
    bash_high = _compile_bash_patterns(patterns_raw.get("high"), "high", "high")
    bash_low = _compile_bash_patterns(patterns_raw.get("low"), "low", "low")

    return PermissionPolicy(
        modes=modes,
        tool_rules=tool_rules,
        bash_blocked=bash_blocked,
        bash_high=bash_high,
        bash_low=bash_low,
        bash_default_risk=default_risk,
        source=config_path,
    )


# 模块级懒加载单例（从默认静态配置装载，只读，绝不回写）。
_POLICY_MISSING = object()
_lazy_policy: Any = _POLICY_MISSING


def default_policy() -> PermissionPolicy:
    """返回仓库内默认静态策略（懒加载并缓存，只读副本）。"""
    global _lazy_policy
    if _lazy_policy is _POLICY_MISSING:
        _lazy_policy = load_permission_policy(None)
    return _lazy_policy


def classify_tool(tool_name: str, tool_input: Optional[Mapping[str, Any]] = None) -> str:
    """把一次工具调用分级为 low/medium/high/blocked 之一。

    - ``bash``：按命令内容联合分级（blocked → high → low → 默认 high）。
    - 其余工具：按工具名匹配静态风险表（glob 通配，精确名优先）。
    - 未识别工具保守返回 ``high``。
    """
    policy = default_policy()
    if not isinstance(tool_name, str) or not tool_name:
        return UNKNOWN_TOOL_RISK
    if tool_name == "bash":
        tool_input = tool_input or {}
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        return policy.classify_bash(str(command))
    risk = policy.classify_tool_name(tool_name)
    if risk is None:
        return UNKNOWN_TOOL_RISK
    return risk


def auto_approve_levels(mode: str) -> FrozenSet[str]:
    """Return the static policy's auto-approval levels for ``mode``.

    This small functional facade mirrors :func:`classify_tool` and keeps
    runtime integrations from reaching into the policy object's internals.
    """
    return default_policy().auto_approve_levels(mode)
