# -*- coding: utf-8 -*-
"""数据解析模块。

负责：
1. 自动发现指定文件夹下 ``srk/`` 子文件夹中的 ``.srk.json`` 文件；
2. 将 ``.srk.json`` 解析为结构化的 :class:`ContestData`；
3. 从各队伍 ``statuses[].solutions[]`` 中汇总全部提交，重建完整提交事件流。

关于 SRK 格式（XCPCIO 榜单快照）：
- ``contest`` : 比赛元信息（标题 / 开始时间 / 时长 / 封榜时长）
- ``problems`` : 题目列表（别名 / 通过数与提交数）
- ``rows`` : 队伍列表，每支队伍含 ``user`` 信息与 ``score``（过题数 / 罚时）
  以及 ``statuses``（每题状态，含该题全部提交 ``solutions``）
- ``sorter`` : 排名规则（罚时单价、不计罚时的结果集合）
- ``series`` : 奖项划分（金 / 银 / 铜数量）
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

#: 判题通过的结果集合
AC_RESULTS = frozenset({"AC", "FB"})

#: 时间单位 -> 毫秒换算表
_UNIT_MS = {
    "ms": 1,
    "s": 1000,
    "min": 60_000,
    "m": 60_000,
    "h": 3_600_000,
    "d": 86_400_000,
}

#: sorter 缺失时使用的默认"不计罚时"结果集合
_DEFAULT_NO_PENALTY = ("FB", "AC", "?", "CE", "UKE", None)


def pair_to_ms(pair: Any, default_ms: int = 0) -> int:
    """将 SRK 中的 ``[数值, 单位]`` 时间对转换为毫秒。

    兼容纯数值（视为毫秒）与缺失单位的情况。
    """
    if isinstance(pair, (list, tuple)):
        if len(pair) < 2:
            return int(pair[0] or 0) if pair else default_ms
        value, unit = pair[0], pair[1]
        return int(value or 0) * _UNIT_MS.get(str(unit).lower(), 1)
    if pair is None:
        return default_ms
    return int(pair)  # 纯数值默认按毫秒处理


@dataclass
class ProblemInfo:
    """题目信息。"""

    alias: str
    accepted: int = 0
    submitted: int = 0


@dataclass
class TeamInfo:
    """队伍信息。"""

    team_id: str
    name: str
    organization: str = ""
    official: bool = True
    members: List[str] = field(default_factory=list)


@dataclass
class SubmissionEvent:
    """一条提交事件（回放的最小单位）。"""

    time_ms: int          # 相对比赛开始的时间（毫秒）
    team_idx: int         # 队伍在 teams 中的下标
    problem_idx: int      # 题目在 problems 中的下标
    result: str           # 判题结果（AC / WA / RTE / TLE / CE ...）
    is_ac: bool           # 是否为通过（AC / FB）


@dataclass
class ContestData:
    """一次比赛的全部回放数据。"""

    title: str
    start_at: str
    duration_ms: int
    frozen_duration_ms: int
    penalty_min: int
    no_penalty: Tuple[Any, ...]
    problems: List[ProblemInfo]
    teams: List[TeamInfo]
    events: List[SubmissionEvent]
    medals: Tuple[int, int, int] = (0, 0, 0)   # (金, 银, 铜) 名次累积边界
    medal_no_tied: bool = False                # 奖牌划分是否展开并列名次
    final_solved: List[int] = field(default_factory=list)   # 快照最终过题数
    final_penalty: List[int] = field(default_factory=list)  # 快照最终罚时
    source_path: str = ""


def find_srk_files(srk_root: str) -> List[str]:
    """在 ``srk_root/srk`` 子文件夹下发现全部 ``.srk.json`` 文件。

    返回按文件名排序的绝对路径列表；目录不存在或为空时返回空列表。
    """
    srk_dir = os.path.join(srk_root, "srk")
    if not os.path.isdir(srk_dir):
        return []
    names = sorted(
        n for n in os.listdir(srk_dir)
        if n.endswith(".srk.json") and os.path.isfile(os.path.join(srk_dir, n))
    )
    return [os.path.join(srk_dir, n) for n in names]


def _pick_title(title_field: Any, fallback: str = "XCPC 榜单回放") -> str:
    """兼容标题字段的多种形态：字符串或 ``{zh-CN, fallback, ...}`` 字典。"""
    if isinstance(title_field, str):
        return title_field
    if isinstance(title_field, dict):
        for key in ("zh-CN", "zh", "en", "fallback"):
            val = title_field.get(key)
            if isinstance(val, str) and val:
                return val
    return fallback


def _pick_pair(pair: Any) -> Any:
    """兼容 ``[值, 单位]`` 与纯数值的时间字段。"""
    if isinstance(pair, (list, tuple)) and len(pair) == 2 and isinstance(pair[1], (str,)):
        return pair
    return pair


def _parse_no_penalty(sorter: Any) -> Tuple[Any, ...]:
    """从 sorter 配置中解析"不计罚时"的结果集合。"""
    if isinstance(sorter, dict):
        config = sorter.get("config") or {}
        lst = config.get("noPenaltyResults")
        if isinstance(lst, list):
            return tuple(lst)
    return _DEFAULT_NO_PENALTY


#: 缺少 ratio 配置时使用的默认奖牌比例（srk 规范 ICPC 默认值）
_DEFAULT_MEDAL_RATIO = (0.1, 0.2, 0.3)


def _parse_medals(series: Any, official_count: int) -> Tuple[Tuple[int, int, int], bool]:
    """从 series 中解析金/银/铜的名次累积边界（取首个含 segments 的系列）。

    依据 srk 规范（ICPC preset）：
    - ``count``：各段固定人数，如 ``[14, 28, 42]`` 表示 14 金 / 28 银 / 42 铜；
    - ``ratio``：各段按正式队伍数 × 比例取整（默认向上取整），如
      ``[0.1, 0.2, 0.3]`` 表示金 ceil(总×0.1) 人、银 ceil(总×0.2) 人、
      铜 ceil(总×0.3) 人；缺少 ``value`` 或 ratio 配置时按默认
      ``[0.1, 0.2, 0.3]`` 计算；
    - 每段人数互不包含（银不包含金、铜不包含前两者），因此转换为
      **名次累积边界**：金第 1 名起共 N0 人、银接续 N1 人、铜再接续 N2 人；
    - 打星队（official=false）不计入分母与段分配；
    - ``noTied`` 为 true 时，段边界按展开并列后的顺序名次计算。

    返回 ``(累积边界, noTied)``。
    """
    if not isinstance(series, list):
        return ((0, 0, 0), False)
    for s in series:
        if not isinstance(s, dict):
            continue
        segments = s.get("segments") or []
        rule = s.get("rule") or {}
        if not segments:
            continue
        options = rule.get("options") or {}

        # count 模式：各段固定人数
        count = options.get("count")
        if isinstance(count, dict):
            vals = count.get("value")
            if isinstance(vals, (list, tuple)) and len(vals) >= 3:
                try:
                    nums = [max(int(v), 0) for v in vals[:3]]
                except (TypeError, ValueError):
                    return ((0, 0, 0), False)
                no_tied = bool(count.get("noTied"))
                return _cumulative_medals(nums, no_tied)

        # ratio 模式：各段 ceil(正式队伍数 × 比例)；缺省按默认比例
        ratio = options.get("ratio")
        if isinstance(ratio, dict) or count is None:
            if not isinstance(ratio, dict):
                ratio = {}  # 缺少 ratio 配置：使用默认比例
            vals = ratio.get("value")
            if not (isinstance(vals, (list, tuple)) and len(vals) >= 3):
                vals = list(_DEFAULT_MEDAL_RATIO)
            rounding = str(ratio.get("rounding") or "ceil").lower()
            no_tied = bool(ratio.get("noTied"))
            nums = []
            for r in vals[:3]:
                try:
                    n = official_count * float(r)
                except (TypeError, ValueError):
                    return ((0, 0, 0), no_tied)
                if rounding == "floor":
                    n = math.floor(n)
                elif rounding == "round":
                    n = math.floor(n + 0.5)
                else:  # ceil（默认）
                    n = math.ceil(n)
                nums.append(max(int(n), 0))
            return _cumulative_medals(nums, no_tied)
    return ((0, 0, 0), False)


def _cumulative_medals(nums: List[int], no_tied: bool) -> Tuple[Tuple[int, int, int], bool]:
    """将各段独立人数转换为名次累积边界。"""
    gold, silver, bronze = nums[0], nums[0] + nums[1], nums[0] + nums[1] + nums[2]
    return ((gold, silver, bronze), no_tied)


def parse_srk(path: str) -> ContestData:
    """解析单个 ``.srk.json`` 文件为 :class:`ContestData`。

    对缺失字段采用保守默认值，尽量保证任一合法 SRK 文件都能被解析；
    解析失败（非法 JSON / 缺少必要字段）时抛出 :class:`ValueError`。
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as fp:  # utf-8-sig 兼容 BOM
            raw: Dict[str, Any] = json.load(fp)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("无法解析文件 %s: %s" % (path, exc)) from exc
    if not isinstance(raw, dict):
        raise ValueError("文件 %s 顶层结构不是 JSON 对象" % path)

    contest = raw.get("contest")
    if not isinstance(contest, dict):
        raise ValueError("文件 %s 缺少 contest 字段" % path)

    title = _pick_title(contest.get("title"))
    start_at = str(contest.get("startAt") or "")
    duration_ms = pair_to_ms(contest.get("duration"))
    frozen_duration_ms = pair_to_ms(contest.get("frozenDuration"))

    sorter = raw.get("sorter") or {}
    penalty_min = 20
    if isinstance(sorter, dict):
        config = sorter.get("config") or {}
        penalty = config.get("penalty")
        if isinstance(penalty, (list, tuple)) and len(penalty) >= 2:
            # 罚时单位为分钟
            penalty_min = int(penalty[0] or 0) or 20
    no_penalty = _parse_no_penalty(sorter)

    problems: List[ProblemInfo] = []
    for p in (raw.get("problems") or []):
        if not isinstance(p, dict):
            continue
        stats = p.get("statistics") or {}
        problems.append(ProblemInfo(
            alias=str(p.get("alias") or "?"),
            accepted=int(stats.get("accepted") or 0),
            submitted=int(stats.get("submitted") or 0),
        ))

    teams: List[TeamInfo] = []
    rows = raw.get("rows") or []
    if not isinstance(rows, list):
        rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        user = row.get("user") or {}
        members = []
        for m in (user.get("teamMembers") or []):
            if isinstance(m, dict) and m.get("name"):
                members.append(str(m["name"]))
        teams.append(TeamInfo(
            team_id=str(user.get("id") or ""),
            name=_pick_title(user.get("name"), fallback="?"),
            organization=str(user.get("organization") or ""),
            official=bool(user.get("official", True)),
            members=members,
        ))

    # 奖牌分母（ratio 模式）：仅正式队伍参与（ICPC 惯例，打星队不计入）
    official_count = sum(1 for t in teams if t.official)
    medals, medal_no_tied = _parse_medals(raw.get("series"), official_count)

    events: List[SubmissionEvent] = []
    n_problems = len(problems)
    for ti, row in enumerate(rows):
        if ti >= len(teams):
            break
        statuses = row.get("statuses") or []
        if not isinstance(statuses, list):
            continue
        for pi, st in enumerate(statuses):
            if pi >= n_problems:
                break
            if not isinstance(st, dict):
                continue
            solns = st.get("solutions") or []
            if isinstance(solns, list) and solns:
                for s in solns:
                    if not isinstance(s, dict):
                        continue
                    res = s.get("result")
                    result = "?" if res is None else str(res)
                    events.append(SubmissionEvent(
                        time_ms=pair_to_ms(s.get("time")),
                        team_idx=ti,
                        problem_idx=pi,
                        result=result,
                        is_ac=res in AC_RESULTS,
                    ))
            else:
                # 兜底：无 solutions 的"汇总快照"格式（如 status 仅含
                # result / time / tries），依据 tries 重建提交事件：
                # - AC 题：tries-1 次失败 + 1 次 AC（AC 时间为 status.time）；
                # - 非 AC 题：tries 次失败。
                # 失败提交时间均匀分布在 [0, status.time)，保证时间线单调，
                # 且 AC 前的失败次数正确，使回放罚时与快照一致。
                res = st.get("result")
                ac = res in AC_RESULTS
                tries = st.get("tries")
                if not ac and not tries:
                    continue
                tries = max(int(tries or 0), 0)
                if tries <= 0:
                    continue
                time_ms = pair_to_ms(st.get("time"))
                n_fail = tries - (1 if ac else 0)
                span = time_ms if time_ms > 0 else 1
                for k in range(1, n_fail + 1):
                    events.append(SubmissionEvent(
                        time_ms=int(span * k / (n_fail + 1)),
                        team_idx=ti,
                        problem_idx=pi,
                        result="WA",
                        is_ac=False,
                    ))
                if ac:
                    events.append(SubmissionEvent(
                        time_ms=time_ms,
                        team_idx=ti,
                        problem_idx=pi,
                        result=str(res),
                        is_ac=True,
                    ))

    # 按时间升序稳定排序，保证时间线单调
    events.sort(key=lambda e: e.time_ms)

    final_solved: List[int] = []
    final_penalty: List[int] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        score = row.get("score") or {}
        final_solved.append(int(score.get("value") or 0))
        final_penalty.append(pair_to_ms(score.get("time")) // 60_000)

    return ContestData(
        title=title,
        start_at=start_at,
        duration_ms=duration_ms or 0,
        frozen_duration_ms=frozen_duration_ms or 0,
        penalty_min=penalty_min,
        no_penalty=no_penalty,
        problems=problems,
        teams=teams,
        events=events,
        medals=medals,
        medal_no_tied=medal_no_tied,
        final_solved=final_solved,
        final_penalty=final_penalty,
        source_path=os.path.abspath(path),
    )
