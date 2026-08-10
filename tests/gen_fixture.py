# -*- coding: utf-8 -*-
"""合成 .srk.json 数据生成器。

用于在多种规模与类型的文件上验证解析与回放的正确性（需求 #7）。
生成流程与真实榜单一致：先模拟一场比赛的完整提交事件流，
再按 ICPC 规则汇总出每队的最终得分，最后写出合法 SRK 文件，
同时返回"真值"（每队过题数与罚时）供测试比对。
"""

from __future__ import annotations

import json
import random
from typing import Any, Dict, List, Tuple

#: 与真实文件一致的"不计罚时"结果
NO_PENALTY = ("FB", "AC", "?", "CE", "UKE", None)
PENALTY_MIN = 20

#: 计罚时的失败结果（与 NO_PENALTY 保持一致，UKE 不计罚时）
WRONG_RESULTS = ("WA", "RTE", "TLE", "MLE", "PE", "OLE")


def _ac_min(ms: int) -> int:
    """通过分钟数（向下取整，对齐 sorter.timeRounding=floor）。"""
    return ms // 60_000


def simulate_contest(n_teams: int, n_problems: int,
                     duration_min: int = 300, seed: int = 1,
                     with_ce: bool = True) -> Tuple[List[Dict[str, Any]], List[Tuple[int, int]]]:
    """模拟一场比赛，返回 (rows, 真值[(solved, penalty), ...])。

    rows 结构对齐 SRK 格式：每行含 user / score / statuses。
    """
    rng = random.Random(seed)
    duration_ms = duration_min * 60_000

    rows: List[Dict[str, Any]] = []
    truth: List[Tuple[int, int]] = []
    # 记录每题首杀，用于写入 FB
    first_ac: Dict[int, int] = {}

    for ti in range(n_teams):
        statuses: List[Dict[str, Any]] = []
        solved = 0
        penalty = 0
        for pi in range(n_problems):
            # 每队每题 ~40% 通过，通过前平均 2 次失败提交
            will_ac = rng.random() < 0.4
            ac_time = 0
            solns: List[Dict[str, Any]] = []
            wrong_before = 0
            if will_ac:
                ac_time = rng.randint(int(duration_ms * 0.02), int(duration_ms * 0.95))
                # 通过前的失败提交（不一定是计罚时结果）
                n_wrong = rng.randint(0, 4)
                times = sorted(rng.randint(int(duration_ms * 0.01), ac_time) for _ in range(n_wrong))
                for t in times:
                    if with_ce and rng.random() < 0.10:
                        result = "CE"                      # 不计罚时
                    else:
                        result = rng.choice(WRONG_RESULTS)  # 计罚时
                        wrong_before += 1
                    solns.append({"result": result, "time": [t, "ms"]})
                solns.append({"result": "AC", "time": [ac_time, "ms"]})
                solved += 1
                penalty += _ac_min(ac_time) + wrong_before * PENALTY_MIN
            else:
                # 未通过：可能提交若干次失败
                n_wrong = rng.randint(0, 3)
                times = sorted(rng.randint(int(duration_ms * 0.01), int(duration_ms * 0.98))
                               for _ in range(n_wrong))
                for t in times:
                    result = rng.choice(WRONG_RESULTS)
                    if with_ce and rng.random() < 0.10:
                        result = "CE"
                    solns.append({"result": result, "time": [t, "ms"]})

            # 未提交状态：不写 solutions
            if not solns:
                statuses.append({"result": None, "time": [0, "s"], "tries": 0})
                continue

            tries = sum(1 for s in solns if s["result"] not in NO_PENALTY)
            status = {
                "result": "AC" if will_ac else "RJ",
                "time": [ac_time // 1000, "s"] if will_ac else [0, "s"],
                "tries": tries,
                "solutions": solns,
            }
            statuses.append(status)
            if will_ac:
                ft = first_ac.get(pi)
                if ft is None or ac_time < first_ac[pi]:
                    first_ac[pi] = ac_time
                    status["result"] = "FB"  # 标记首杀
                    solns[-1]["result"] = "FB"

        rows.append({
            "user": {
                "name": "队伍%03d" % ti,
                "id": "T-%03d" % ti,
                "organization": "学校%d" % (ti % 8),
                "official": ti % 7 != 0,
            },
            "score": {"value": solved, "time": [penalty * 60, "s"]},
            "statuses": statuses,
        })
        truth.append((solved, penalty))

    # rows 按 (过题数降序, 罚时升序) 排序，模拟真实榜单文件
    order = sorted(range(n_teams),
                   key=lambda i: (-truth[i][0], truth[i][1]))
    rows = [rows[i] for i in order]
    truth = [truth[i] for i in order]
    return rows, truth


def write_srk(path: str, rows: List[Dict[str, Any]], n_problems: int,
              title: str = "合成测试赛", start_at: str = "2026-01-01T10:00:00+08:00") -> str:
    """将 rows 写为合法 SRK 文件，返回文件路径。"""
    data = {
        "type": "general",
        "version": "0.3.13",
        "contest": {
            "title": {"zh-CN": title, "fallback": title},
            "startAt": start_at,
            "duration": [5, "h"],
            "frozenDuration": [0, "h"],
        },
        "problems": [
            {"alias": chr(ord("A") + i),
             "statistics": {"accepted": 0, "submitted": 0}}
            for i in range(n_problems)
        ],
        "series": [
            {"title": "#", "segments": [
                {"title": "金", "style": "gold"},
                {"title": "银", "style": "silver"},
                {"title": "铜", "style": "bronze"}],
             "rule": {"preset": "ICPC",
                      "options": {"count": {"value": [3, 6, 9], "noTied": True}}}},
        ],
        "rows": rows,
        "sorter": {
            "algorithm": "ICPC",
            "config": {
                "noPenaltyResults": list(NO_PENALTY),
                "penalty": [PENALTY_MIN, "min"],
                "timePrecision": "min",
                "timeRounding": "floor",
            },
        },
    }
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False)
    return path


def make_no_solutions(path: str) -> List[Tuple[int, int]]:
    """边界文件：solutions 缺失但状态为已通过（测试兜底合成事件）。

    此场景下 AC 题无失败提交，真值可直接确定：罚时 = 通过分钟数。
    返回真值列表（与写入文件的行序一致）。
    """
    n_teams, n_problems = 8, 3
    rows: List[Dict[str, Any]] = []
    truth: List[Tuple[int, int]] = []
    for ti in range(n_teams):
        statuses = []
        solved = penalty = 0
        for pi in range(n_problems):
            will_ac = (ti + pi) % 3 == 0
            if will_ac:
                ac_time = (60 + ti * 60 + pi * 30) * 1000  # 确定性时间
                statuses.append({
                    "result": "AC",
                    "time": [ac_time // 1000, "s"],
                    "tries": 1,
                })
                solved += 1
                penalty += _ac_min(ac_time)
            else:
                statuses.append({"result": None, "time": [0, "s"], "tries": 0})
        rows.append({
            "user": {"name": "队伍%03d" % ti, "id": "T-%03d" % ti,
                     "organization": "学校%d" % (ti % 3), "official": True},
            "score": {"value": solved, "time": [penalty * 60, "s"]},
            "statuses": statuses,
        })
        truth.append((solved, penalty))
    # 按 (过题数降序, 罚时升序) 排序，真值同步排序
    order = sorted(range(n_teams), key=lambda i: (-truth[i][0], truth[i][1]))
    rows = [rows[i] for i in order]
    truth = [truth[i] for i in order]
    write_srk(path, rows, n_problems, title="缺 solutions")
    return truth


def make_fixtures(srk_dir: str) -> List[Tuple[str, str, List[Tuple[int, int]]]]:
    """生成一组不同规模 / 类型的合成 SRK 文件。

    Returns:
        列表 [(文件名, 文件路径, 真值列表)]，真值与文件行序一一对应。
    """
    import os
    os.makedirs(srk_dir, exist_ok=True)
    fixtures: List[Tuple[str, str, List[Tuple[int, int]]]] = []

    def gen(name: str, n_teams: int, n_problems: int, seed: int,
            with_ce: bool = True, duration_min: int = 300) -> None:
        rows, truth = simulate_contest(n_teams, n_problems, duration_min, seed, with_ce)
        p = os.path.join(srk_dir, name)
        write_srk(p, rows, n_problems, title=name)
        fixtures.append((name, p, truth))

    gen("small.srk.json", 12, 5, seed=1, with_ce=False)
    gen("medium.srk.json", 60, 10, seed=2)
    gen("large.srk.json", 300, 13, seed=3)
    gen("ties.srk.json", 20, 6, seed=4)   # 大量同分场景

    # 边界：空比赛（无队伍无题目）
    p = os.path.join(srk_dir, "empty.srk.json")
    write_srk(p, [], 0, title="空比赛")
    fixtures.append(("empty.srk.json", p, []))

    # 边界：solutions 缺失但状态为已通过
    p = os.path.join(srk_dir, "no_solutions.srk.json")
    truth = make_no_solutions(p)
    fixtures.append(("no_solutions.srk.json", p, truth))

    return fixtures
