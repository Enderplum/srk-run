# -*- coding: utf-8 -*-
"""XCPC 榜单回放 — 单元测试。

运行方式：
    python -m unittest discover -s tests -v
或：
    python tests/test_replay.py
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

# 保证可从仓库根目录或 tests 目录两种方式运行
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

sys.path.insert(0, str(REPO_ROOT / "tests"))

from xcpc_replay import parser, replay, server  # noqa: E402
from gen_fixture import make_fixtures  # noqa: E402

class TestSrkFiles(unittest.TestCase):
    """对 srk/ 目录下所有 .srk.json 的通用性校验（不针对单一文件）。

    数据文件 *.srk.json 不纳入版本控制，若 srk/ 目录下没有文件
    （例如全新 clone 后未自行放置数据），相关用例自动跳过。
    新增任意 .srk.json 数据后无需修改本测试即可获得覆盖。
    """

    @classmethod
    def setUpClass(cls):
        # 使用与生产代码相同的发现逻辑，保证口径一致
        cls.files = parser.find_srk_files(str(REPO_ROOT))
        if not cls.files:
            raise unittest.SkipTest("srk/ 目录下没有 .srk.json 文件，跳过")

    def test_general_props(self):
        """每个文件：结构一致、事件按时间有序。

        事件总数与 solutions 数量相等仅对含提交明细（solutions）的
        文件成立；无 solutions 的"汇总快照"文件由 parser 依据
        tries 重建事件，二者不对等。
        """
        for path in self.files:
            with self.subTest(file=os.path.basename(path)):
                data = parser.parse_srk(path)
                with open(path, encoding="utf-8") as fp:
                    raw = json.load(fp)
                # 队伍数与 rows 行数一致
                self.assertEqual(len(data.teams), len(raw.get("rows") or []))
                # 含提交明细时：事件总数 = 全部 solutions 数量
                total_solns = sum(
                    len(st.get("solutions") or [])
                    for row in raw.get("rows") or []
                    for st in row.get("statuses") or []
                    if isinstance(st, dict)
                )
                if total_solns:
                    self.assertEqual(len(data.events), total_solns)
                # 事件按时间单调有序
                times = [e.time_ms for e in data.events]
                self.assertEqual(times, sorted(times))

    def test_replay_matches_snapshot(self):
        """每个文件：回放结果与文件内快照完全一致。"""
        for path in self.files:
            with self.subTest(file=os.path.basename(path)):
                data = parser.parse_srk(path)
                issues = replay.validate_against_snapshot(data)
                self.assertEqual(issues, [], "回放与快照不一致: %s" % issues[:5])

    def test_rank_order_matches_file(self):
        """每个文件：榜单名次顺序与文件 rows 顺序一致。"""
        for path in self.files:
            with self.subTest(file=os.path.basename(path)):
                data = parser.parse_srk(path)
                scorer = replay.replay_snapshot(data)
                ranked = replay.rank_teams(scorer)
                with open(path, encoding="utf-8") as fp:
                    file_order = [r["score"]["value"] for r in json.load(fp)["rows"]]
                self.assertEqual([e[2] for e in ranked], file_order)

    def test_medals(self):
        """每个文件：奖牌名次边界与 series 配置一致。

        各段人数互不包含（银/铜不包含前段），parser 输出名次累积边界：
        ``count`` 各段取固定人数；``ratio`` 各段取 ceil(正式队伍数 × 比例)；
        打星队不计入分母。
        """
        for path in self.files:
            with self.subTest(file=os.path.basename(path)):
                data = parser.parse_srk(path)
                with open(path, encoding="utf-8") as fp:
                    raw = json.load(fp)
                official = sum(1 for t in data.teams if t.official)
                expected = (0, 0, 0)
                for s in raw.get("series") or []:
                    if not isinstance(s, dict) or not (s.get("segments") or []):
                        continue
                    opts = (s.get("rule") or {}).get("options") or {}
                    cnt = opts.get("count")
                    if isinstance(cnt, dict):
                        vals = cnt.get("value")
                        if isinstance(vals, (list, tuple)) and len(vals) >= 3:
                            nums = [max(int(v), 0) for v in vals[:3]]
                            expected = (nums[0], nums[0] + nums[1], nums[0] + nums[1] + nums[2])
                        break
                    rat = opts.get("ratio")
                    if isinstance(rat, dict):
                        vals = rat.get("value")
                        if not (isinstance(vals, (list, tuple)) and len(vals) >= 3):
                            vals = [0.1, 0.2, 0.3]
                        rounding = str(rat.get("rounding") or "ceil").lower()
                        nums = []
                        for r in vals[:3]:
                            n = official * float(r)
                            if rounding == "floor":
                                n = math.floor(n)
                            elif rounding == "round":
                                n = math.floor(n + 0.5)
                            else:
                                n = math.ceil(n)
                            nums.append(max(int(n), 0))
                        expected = (nums[0], nums[0] + nums[1], nums[0] + nums[1] + nums[2])
                        break
                self.assertEqual(data.medals, expected,
                                 "奖牌名次边界与 series 配置不一致")


class TestSyntheticFixtures(unittest.TestCase):
    """在不同规模 / 类型的合成文件上验证解析与回放（需求 #7）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.files = {name: (path, truth)
                     for name, path, truth in make_fixtures(cls.tmp.name)}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _check_file(self, name):
        path, truth = self.files[name]
        data = parser.parse_srk(path)
        self.assertEqual(len(data.teams), len(truth))
        self.assertEqual(replay.validate_against_snapshot(data), [],
                         "%s 回放与文件快照不一致" % name)
        # 与生成真值比对
        scorer = replay.replay_snapshot(data)
        for ti, (solved, penalty) in enumerate(truth):
            self.assertEqual(scorer.solved[ti], solved, "%s 队伍%d 过题数" % (name, ti))
            self.assertEqual(scorer.penalty[ti], penalty, "%s 队伍%d 罚时" % (name, ti))

    def test_small(self): self._check_file("small.srk.json")
    def test_medium(self): self._check_file("medium.srk.json")
    def test_large(self): self._check_file("large.srk.json")
    def test_ties(self): self._check_file("ties.srk.json")
    def test_no_solutions(self): self._check_file("no_solutions.srk.json")

    def test_empty(self):
        path, _ = self.files["empty.srk.json"]
        data = parser.parse_srk(path)
        self.assertEqual(len(data.teams), 0)
        self.assertEqual(len(data.problems), 0)
        self.assertEqual(len(data.events), 0)
        self.assertEqual(replay.validate_against_snapshot(data), [])


class TestEdgeCases(unittest.TestCase):
    def test_no_srk_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(parser.find_srk_files(tmp), [])

    def test_malformed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "x.srk.json")
            with open(bad, "w") as fp:
                fp.write("{not json")
            with self.assertRaises(ValueError):
                parser.parse_srk(bad)

    def test_missing_contest(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "x.srk.json")
            with open(bad, "w") as fp:
                json.dump({"problems": []}, fp)
            with self.assertRaises(ValueError):
                parser.parse_srk(bad)

    def test_utf8_bom(self):
        """兼容带 BOM 的 UTF-8 文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "bom.srk.json")
            with open(p, "wb") as fp:
                fp.write(b"\xef\xbb\xbf" + json.dumps({
                    "contest": {"title": "x", "startAt": "", "duration": [1, "h"],
                                "frozenDuration": [0, "h"]},
                    "problems": [{"alias": "A", "statistics": {}}],
                    "rows": [],
                }).encode("utf-8"))
            data = parser.parse_srk(p)
            self.assertEqual(data.title, "x")

    def test_ratio_medals(self):
        """ratio 模式奖牌：各段 ceil(正式队伍数 × 比例)，互不包含（累积边界），打星队不计入分母。"""
        with tempfile.TemporaryDirectory() as tmp:
            rows = []
            for i in range(4):   # 4 支正式队伍
                rows.append({"user": {"id": str(i), "name": "T%d" % i,
                                      "official": True},
                             "score": {"value": 0}, "statuses": []})
            rows.append({"user": {"id": "99", "name": "Ghost", "official": False},   # 1 支打星队
                         "score": {"value": 0}, "statuses": []})
            doc = {
                "version": "0.3.12",
                "contest": {"title": "ratio-test", "startAt": "",
                            "duration": [1, "h"], "frozenDuration": [0, "h"]},
                "problems": [{"alias": "A", "statistics": {}}],
                "rows": rows,
                # 5 支队伍中仅 4 支正式：每段 4×0.25=1 → ceil=1 人
                # 名次累积边界：金 1 名、银到 2 名、铜到 3 名
                "series": [{"title": "#", "segments": [
                    {"title": "G", "style": "gold"},
                    {"title": "S", "style": "silver"},
                    {"title": "B", "style": "bronze"}],
                    "rule": {"preset": "ICPC",
                             "options": {"ratio": {"value": [0.25, 0.25, 0.25]}}}}],
            }
            p = os.path.join(tmp, "ratio.srk.json")
            with open(p, "w", encoding="utf-8") as fp:
                json.dump(doc, fp)
            data = parser.parse_srk(p)
            self.assertEqual(data.medals, (1, 2, 3),
                             "应只按正式队伍数计算（每段 4×0.25=1 人），并转累积边界")

    def test_default_ratio_medals(self):
        """series 缺少 ratio/count 配置时，按默认比例 0.1/0.2/0.3 计算奖牌。"""
        with tempfile.TemporaryDirectory() as tmp:
            rows = [{"user": {"id": str(i), "name": "T%d" % i},
                     "score": {"value": 0}, "statuses": []} for i in range(10)]
            doc = {
                "version": "0.3.12",
                "contest": {"title": "default-ratio", "startAt": "",
                            "duration": [1, "h"], "frozenDuration": [0, "h"]},
                "problems": [{"alias": "A", "statistics": {}}],
                "rows": rows,
                "series": [{"title": "#", "segments": [
                    {"title": "G", "style": "gold"},
                    {"title": "S", "style": "silver"},
                    {"title": "B", "style": "bronze"}],
                    "rule": {"preset": "ICPC", "options": {}}}],
            }
            p = os.path.join(tmp, "default_ratio.srk.json")
            with open(p, "w", encoding="utf-8") as fp:
                json.dump(doc, fp)
            data = parser.parse_srk(p)
            # 10 × 0.1/0.2/0.3 → 1/2/3 人 → 累积边界 (1, 3, 6)
            self.assertEqual(data.medals, (1, 3, 6),
                             "缺省比例应为 0.1/0.2/0.3 并转累积边界")


class TestPayload(unittest.TestCase):
    def test_build_payload(self):
        """对 srk/ 下每个文件：payload 结构完整、事件单调有序、快照一致。"""
        files = parser.find_srk_files(str(REPO_ROOT))
        if not files:
            self.skipTest("srk/ 目录下没有 .srk.json 文件，跳过")
        for path in files:
            with self.subTest(file=os.path.basename(path)):
                data = parser.parse_srk(path)
                payload = replay.build_payload(data)
                for key in ("title", "durationMs", "penaltyMin", "problems",
                            "teams", "events", "finalBoard", "medals"):
                    self.assertIn(key, payload)
                self.assertEqual(len(payload["events"]), len(data.events))
                self.assertEqual(len(payload["teams"]), len(data.teams))
                self.assertEqual(payload["finalBoard"]["solved"], data.final_solved)
                # 事件时间单调
                ts = [e["t"] for e in payload["events"]]
                self.assertEqual(ts, sorted(ts))

    def test_ranking_ties(self):
        """并列同名次、名次顺延。"""
        scorer = replay.ICPCScorer(4, 1, penalty_min=20)
        evs = [
            parser.SubmissionEvent(60_000, 0, 0, "WA", False),
            parser.SubmissionEvent(120_000, 0, 0, "AC", True),   # 队0: 1题, 罚时2+20
            parser.SubmissionEvent(90_000, 1, 0, "AC", True),     # 队1: 1题, 罚时1
            parser.SubmissionEvent(80_000, 2, 0, "AC", True),     # 队2: 1题, 罚时1 -> 与队1同分
        ]
        for e in evs:
            scorer.apply(e)
        ranked = replay.rank_teams(scorer)
        # 队1、队2 并列第 1，队0 第 3，队3（未解题）第 4
        self.assertEqual([(r[1], r[0]) for r in ranked],
                         [(1, 1), (2, 1), (0, 3), (3, 4)])


class TestServerAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        make_fixtures(os.path.join(cls.tmp.name, "srk"))
        cls.httpd = server.create_server(cls.tmp.name, port=0, quiet=True)
        import threading
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def _get(self, url):
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_files_api(self):
        data = self._get(self.base + "/api/files")
        self.assertIn("small.srk.json", data["files"])
        self.assertIn("large.srk.json", data["files"])

    def test_contest_api(self):
        data = self._get(self.base + "/api/contest?file=small.srk.json")
        self.assertEqual(data["title"], "small.srk.json")
        self.assertEqual(len(data["teams"]), 12)
        self.assertEqual(len(data["problems"]), 5)

    def test_contest_api_rejects_bad_file(self):
        """拒绝目录穿越等非法文件名。"""
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError):
            self._get(self.base + "/api/contest?file=../secret.srk.json")

    def test_index_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as resp:
            body = resp.read().decode("utf-8")
        self.assertIn("XCPC", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
