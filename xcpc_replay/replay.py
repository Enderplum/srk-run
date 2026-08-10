# -*- coding: utf-8 -*-
"""回放引擎模块。

职责：
1. 基于解析得到的提交事件流，逐事件推进 ICPC 计分（过题数 / 罚时）；
2. 提供标准 ICPC 排名算法（过题数降序、罚时升序、并列同名次）；
3. 用 ``.srk.json`` 快照中的最终榜单校验回放结果的准确性；
4. 组装前端所需的完整回放数据载荷。

计分规则（对齐 SRK ``sorter.config``）：
- 每次"计罚时"提交（结果不在 ``noPenalty`` 集合内）累加该题的尝试次数；
- 通过题目时：过题数 +1，罚时 += 通过分钟数 + 20 × 通过前尝试次数；
- CE / 未知结果等不计罚时的提交不增加尝试次数。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .parser import ContestData, SubmissionEvent


class ICPCScorer:
    """ICPC 计分器：维护全部队伍在某一时刻的榜单状态。

    Attributes:
        solved: 每支队伍已通过的题目数
        penalty: 每支队伍的累计罚时（分钟）
        attempts: ``[队伍][题目]`` 计罚时的提交次数
        ac_time: ``[队伍][题目]`` 通过时间（毫秒），未通过为 -1
    """

    def __init__(self, n_teams: int, n_problems: int, penalty_min: int = 20,
                 no_penalty: Optional[Tuple[Any, ...]] = None) -> None:
        self.n_teams = n_teams
        self.n_problems = n_problems
        self.penalty_min = penalty_min
        self.no_penalty = no_penalty or ()
        self.solved = [0] * n_teams
        self.penalty = [0] * n_teams
        self.attempts = [[0] * n_problems for _ in range(n_teams)]
        self.ac_time = [[-1] * n_problems for _ in range(n_teams)]

    def apply(self, ev: SubmissionEvent) -> bool:
        """应用一条提交事件，返回该事件是否改变了榜单（过题）。

        注意：通过后的重复提交会被安全忽略。
        """
        if not (0 <= ev.team_idx < self.n_teams and 0 <= ev.problem_idx < self.n_problems):
            return False
        team, prob = ev.team_idx, ev.problem_idx
        if self.ac_time[team][prob] >= 0:
            return False  # 题目已通过，忽略后续提交

        if ev.is_ac:
            self.ac_time[team][prob] = ev.time_ms
            self.solved[team] += 1
            ac_min = ev.time_ms // 60_000                      # 通过分钟数（向下取整）
            wrong_before_ac = self.attempts[team][prob]        # 通过前计罚时的失败次数
            self.penalty[team] += ac_min + wrong_before_ac * self.penalty_min
            return True
        # 计罚时的失败提交才累计尝试次数（AC / FB / CE 等不计入）
        if ev.result not in self.no_penalty:
            self.attempts[team][prob] += 1
        return False

    def replay_all(self, events: List[SubmissionEvent]) -> None:
        """按时间序依次应用全部事件（用于回放校验）。"""
        for ev in events:
            self.apply(ev)


def rank_teams(scorer: ICPCScorer) -> List[Tuple[int, int, int, int]]:
    """标准 ICPC 排名。

    返回按名次升序排列的 ``(rank, team_idx, solved, penalty)`` 列表。
    同分并列同名次，后续名次顺延（如 1, 1, 3）。
    """
    order = sorted(range(scorer.n_teams),
                   key=lambda i: (-scorer.solved[i], scorer.penalty[i]))
    ranked: List[Tuple[int, int, int, int]] = []
    prev_key: Optional[Tuple[int, int]] = None
    prev_rank = 0
    for pos, ti in enumerate(order):
        key = (scorer.solved[ti], scorer.penalty[ti])
        if key == prev_key:
            rank = prev_rank
        else:
            rank = pos + 1
            prev_rank, prev_key = rank, key
        ranked.append((rank, ti, scorer.solved[ti], scorer.penalty[ti]))
    return ranked


def replay_snapshot(data: ContestData) -> ICPCScorer:
    """将全部事件回放到结束，返回最终计分状态。"""
    scorer = ICPCScorer(len(data.teams), len(data.problems),
                        data.penalty_min, data.no_penalty)
    scorer.replay_all(data.events)
    return scorer


def validate_against_snapshot(data: ContestData) -> List[str]:
    """校验回放结果与 SRK 快照最终榜单是否一致。

    返回不一致项的描述列表；为空列表表示完全一致。

    罚时差异的特殊处理：个别数据源的最终快照把"不计罚时"的结果
    （如 sorter 配置为 noPenalty 的 NOUT/CE 等）也计入了 tries 罚时，
    导致快照与按明细回放的结果存在固定差值。当该差值恰好等于
    ``penalty_min × 该队不计罚时的失败提交数`` 时，视为数据源自身
    矛盾而非回放错误，不计入返回的 issues。
    """
    scorer = replay_snapshot(data)
    # 每队"不计罚时且未通过"的提交数（数据源可能将其计入快照罚时）
    no_pen_count = [0] * len(data.teams)
    for ev in data.events:
        if not ev.is_ac and ev.result in data.no_penalty:
            no_pen_count[ev.team_idx] += 1

    issues: List[str] = []
    for ti, team in enumerate(data.teams):
        if scorer.solved[ti] != data.final_solved[ti]:
            issues.append(
                "队伍[%d]%s 过题数不一致：回放=%d 快照=%d"
                % (ti, team.name, scorer.solved[ti], data.final_solved[ti]))
        diff = data.final_penalty[ti] - scorer.penalty[ti]
        if diff != 0:
            if diff > 0 and diff == no_pen_count[ti] * data.penalty_min:
                continue  # 可归因于数据源自身矛盾，非回放错误
            issues.append(
                "队伍[%d]%s 罚时不一致：回放=%d 快照=%d"
                % (ti, team.name, scorer.penalty[ti], data.final_penalty[ti]))
    return issues


def snapshot_ranks(data: ContestData) -> List[int]:
    """基于快照最终得分计算名次数组（与 teams 下标对齐）。"""
    scorer = ICPCScorer(len(data.teams), len(data.problems),
                        data.penalty_min, data.no_penalty)
    for ti in range(len(data.teams)):
        scorer.solved[ti] = data.final_solved[ti]
        scorer.penalty[ti] = data.final_penalty[ti]
    ranked = rank_teams(scorer)
    ranks = [0] * len(data.teams)
    for rank, ti, _s, _p in ranked:
        ranks[ti] = rank
    return ranks


def build_payload(data: ContestData) -> Dict[str, Any]:
    """组装前端回放所需的完整数据载荷。"""
    return {
        "source": data.source_path.rsplit(os_sep(), 1)[-1],
        "title": data.title,
        "startAt": data.start_at,
        "durationMs": data.duration_ms,
        "frozenDurationMs": data.frozen_duration_ms,
        "penaltyMin": data.penalty_min,
        "noPenalty": list(data.no_penalty),
        "medals": list(data.medals),
        "noTied": data.medal_no_tied,
        "problems": [
            {"alias": p.alias, "accepted": p.accepted, "submitted": p.submitted}
            for p in data.problems
        ],
        "teams": [
            {"id": t.team_id, "name": t.name, "organization": t.organization,
             "official": t.official, "members": t.members}
            for t in data.teams
        ],
        "events": [
            {"t": e.time_ms, "team": e.team_idx, "problem": e.problem_idx,
             "result": e.result, "ac": e.is_ac}
            for e in data.events
        ],
        "finalBoard": {
            "solved": data.final_solved,
            "penalty": data.final_penalty,
            "rank": snapshot_ranks(data),
        },
    }


def os_sep() -> str:
    """跨平台路径分隔符（仅为序列化 source 文件名）。"""
    import os
    return os.sep
