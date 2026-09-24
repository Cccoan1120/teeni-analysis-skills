import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

from publisher.opening_cohorts import (
    DIRECTION_KEYS, DIRECTION_LABELS, RULES, build_addon, classify, file_sha256, is_reply,
    validate_opening_cohorts, verify_addon, write_package,
)


class OpeningCohortsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def package(self, sessions, day="2026-09-13", mutate=None):
        folder = self.root / day
        folder.mkdir(exist_ok=True)
        rows = []
        for cid, texts in sessions.items():
            for turn, text in enumerate(texts):
                rows.append(dict(id=str(len(rows) + 1), cid=cid, clientId="user-" + cid,
                                 source_row=len(rows) + 2, sceneId="488", created_at=day + " 12:00:00",
                                 timestamp=str(turn), text=text))
        if mutate:
            mutate(rows)
        detail = folder / "detail.csv"
        with detail.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["id", "cid", "clientId", "source_row", "sceneId", "created_at", "timestamp", "text"])
            writer.writeheader()
            writer.writerows(rows)
        source, workbook = folder / "source.csv", folder / "workbook.xlsx"
        with source.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["cid", "text", "response"], delimiter="\t")
            writer.writeheader()
            for row in sorted(rows, key=lambda item: int(item["source_row"])):
                opening = "知识计划==【system】\nsynthetic" if row["text"].strip() == RULES["placeholder"] else ""
                writer.writerow({"cid": row["cid"], "text": row["text"], "response": json.dumps({"extra": {"opening_prompt": opening}}, ensure_ascii=False)})
        workbook.write_bytes(b"synthetic workbook")
        manifest = dict(schemaVersion="teeni-base-bundle-manifest/1.0.0", mode="primary", dataDate=day, sceneId="488",
                        sources=[dict(role="primary", file=source.name, sha256=file_sha256(source), rows=len(rows), sceneId="488")],
                        outputs=[dict(role="primary_detail", file=detail.name, sha256=file_sha256(detail), rows=len(rows)),
                                 dict(role="workbook", file=workbook.name, sha256=file_sha256(workbook))])
        manifest_path = folder / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        # Do not use the helper here: corruption tests must reach build_addon validation.
        return dict(status="local_verified", productVersion="M1", dataDate=day, sceneId="488", rowCount=len(rows),
                    detailPath=str(detail), detailSha256=file_sha256(detail), sourcePath=str(source), sourceSha256=file_sha256(source),
                    workbookPath=str(workbook), workbookSha256=file_sha256(workbook), manifestPath=str(manifest_path), manifestSha256=file_sha256(manifest_path))

    def test_frozen_matching_and_short_replies(self):
        self.assertEqual(classify(" \t##{startPrompt}##\n"), "startPrompt")
        self.assertEqual(classify("##{STARTPROMPT}##"), "other")
        self.assertEqual(classify("围绕今天的话题聊一聊"), "other")
        self.assertEqual(classify("围绕这个内容，你可以问我一个相关的问题吗？直接展示一句话简洁问题内容。"), "legacyDefault")
        self.assertEqual(classify("和我打招呼并称呼我的名字：小明"), "legacyDefault")
        self.assertEqual(classify("和我打招呼，并称呼我的名字：小明"), "other")
        for text in ("嗯", "对", "你好", "围绕太阳聊聊"):
            self.assertTrue(is_reply(text))
        for text in ("", " ", RULES["placeholder"], *RULES["legacyExact"], *RULES["systemHints"]):
            self.assertFalse(is_reply(text))

    def test_continuous_templates_and_first_only_classification(self):
        ready = self.package({"a": [RULES["placeholder"], RULES["legacyExact"][0]],
                              "b": [RULES["legacyExact"][1], "嗯"],
                              "c": ["问个问题"], "d": ["用户开头", RULES["placeholder"], "对"],
                              "e": [RULES["placeholder"], "对", "a", "b", "c"]})
        value = build_addon(ready)
        start, legacy, other = value["groups"]
        self.assertEqual((start["totalSessions"], start["totalTurns"], start["replySessions"], start["multiTurnSessions"], start["fivePlusSessions"]), (2, 7, 1, 2, 1))
        self.assertEqual((legacy["totalSessions"], legacy["replySessions"]), (1, 1))
        self.assertEqual((other["totalSessions"], other["replySessions"]), (2, 1))
        self.assertEqual([item["key"] for item in value["startPromptDirections"]], list(DIRECTION_KEYS))
        self.assertEqual([(item["label"], item["totalSessions"]) for item in value["startPromptDirections"] if item["totalSessions"]], [("知识计划", 2)])
        self.assertEqual(verify_addon(value, ready)["status"], "passed")

    def test_direction_states_reconcile_with_start_prompt(self):
        ready = self.package({"a": [RULES["placeholder"], "对"], "b": [RULES["placeholder"]], "c": [RULES["placeholder"], "嗯"]})
        source = Path(ready["sourcePath"])
        with source.open("r", encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
        rows[0]["response"] = json.dumps({"extra": {"opening_prompt": "高频兴趣==【system】\nx"}}, ensure_ascii=False)
        rows[2]["response"] = json.dumps({"extra": {"opening_prompt": "未调用大模型，无大模型提示词\n\n【生日文案】\n生日快乐\n\n【最终文案】\n生日快乐"}}, ensure_ascii=False)
        rows[3]["response"] = json.dumps({"extra": {"opening_prompt": "运营计划/运营任务+全国爱牙日==【system】\nx"}}, ensure_ascii=False)
        with source.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["cid", "text", "response"], delimiter="\t")
            writer.writeheader(); writer.writerows(rows)
        ready["sourceSha256"] = file_sha256(source)
        manifest = json.loads(Path(ready["manifestPath"]).read_text())
        manifest["sources"][0]["sha256"] = ready["sourceSha256"]
        Path(ready["manifestPath"]).write_text(json.dumps(manifest))
        ready["manifestSha256"] = file_sha256(ready["manifestPath"])
        value = build_addon(ready)
        self.assertEqual({item["key"]: item["totalSessions"] for item in value["startPromptDirections"] if item["totalSessions"]}, {"preference": 1, "operation": 1})
        self.assertEqual(value["directionCoverage"], dict(totalStartPromptSessions=3, classifiedSessions=2,
                                                           unlabelledSessions=1, unsupportedPrefixSessions=0,
                                                           coverageRate=2 / 3))
        self.assertLess(sum(item["totalTurns"] for item in value["startPromptDirections"]), value["groups"][0]["totalTurns"])
        self.assertEqual(verify_addon(value, ready)["status"], "passed")

    def test_general_interest_prefix_uses_precomputed_opening_without_model_prompt(self):
        ready = self.package({"generic": [RULES["placeholder"], "嗯"]})
        source = Path(ready["sourcePath"])
        with source.open("r", encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
        rows[0]["response"] = json.dumps({"extra": {"opening_prompt": (
            "通用兴趣==未调用大模型，无大模型提示词 【预写通用兴趣文案】 "
            "嗨，蛋仔圆圆的，像一颗会走路的彩蛋。要给它一个小背包，你想装进什么？ "
            "【最终文案】 嗨，一一，蛋仔圆圆的，像一颗会走路的彩蛋。要给它一个小背包，你想装进什么？"
        )}}, ensure_ascii=False)
        with source.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["cid", "text", "response"], delimiter="\t")
            writer.writeheader(); writer.writerows(rows)
        ready["sourceSha256"] = file_sha256(source)
        manifest = json.loads(Path(ready["manifestPath"]).read_text())
        manifest["sources"][0]["sha256"] = ready["sourceSha256"]
        Path(ready["manifestPath"]).write_text(json.dumps(manifest))
        ready["manifestSha256"] = file_sha256(ready["manifestPath"])
        value = build_addon(ready)
        self.assertEqual({item["key"]: item["totalSessions"] for item in value["startPromptDirections"] if item["totalSessions"]}, {"general_interest": 1})
        self.assertEqual(value["directionCoverage"]["classifiedSessions"], 1)
        self.assertEqual(verify_addon(value, ready)["status"], "passed")

    def test_missing_opening_prompt_remains_unlabelled(self):
        ready = self.package({"missing": [RULES["placeholder"]], "known": [RULES["placeholder"]]})
        source = Path(ready["sourcePath"])
        with source.open("r", encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
        rows[0]["response"] = json.dumps({"generated_text": "No opening metadata"})
        with source.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["cid", "text", "response"], delimiter="\t")
            writer.writeheader(); writer.writerows(rows)
        ready["sourceSha256"] = file_sha256(source)
        manifest = json.loads(Path(ready["manifestPath"]).read_text())
        manifest["sources"][0]["sha256"] = ready["sourceSha256"]
        Path(ready["manifestPath"]).write_text(json.dumps(manifest))
        ready["manifestSha256"] = file_sha256(ready["manifestPath"])
        value = build_addon(ready)
        self.assertEqual(value["directionCoverage"], dict(totalStartPromptSessions=2,
                         classifiedSessions=1, unlabelledSessions=1,
                         unsupportedPrefixSessions=0, coverageRate=0.5))
        self.assertEqual(verify_addon(value, ready)["status"], "passed")

    def test_only_prefix_before_first_separator_is_classified(self):
        samples = {
            "morning": "未调用大模型，无大模型提示词\n\n【候选原文（按现有规则处理复述前缀、问句和长度）】\n小明，早上好呀，我们又见面啦！昨晚睡得好吗？\n\n【最终文案】\n小明，早上好呀，我们又见面啦！昨晚睡得好吗？",
            "midday": "未调用大模型，无大模型提示词\n\n【候选原文（按现有规则处理复述前缀、问句和长度）】\n小明，中午好呀，又见到你真开心！今天上午过得怎么样？\n\n【最终文案】\n同上",
            "afternoon": "未调用大模型，无大模型提示词\n\n【候选原文（按现有规则处理复述前缀、问句和长度）】\n小明，下午好呀，我们又碰面啦！今天有没有遇到什么好玩的事？\n\n【最终文案】\n同上",
            "night": "未调用大模型，无大模型提示词\n\n【候选原文（按现有规则处理复述前缀、问句和长度）】\n已经很晚啦，要不要聊一个轻松的小故事？\n\n【最终文案】\n同上",
            "operation": "未调用大模型，无大模型提示词\n\n【候选原文（按现有规则处理复述前缀、问句和长度）】\n十分钟后要叫你哦，记得回来哈，你愿意和我聊聊吗？\n\n【最终文案】\n同上",
            "unfinished": "未调用大模型，无大模型提示词\n\n【候选原文（按现有规则处理复述前缀、问句和长度）】\n上次的故事还没讲完，要接着讲吗？\n\n【最终文案】\n同上",
        }
        ready = self.package({key: [RULES["placeholder"]] for key in samples})
        source = Path(ready["sourcePath"])
        with source.open("r", encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
        for row in rows:
            row["response"] = json.dumps({"extra": {"opening_prompt": samples[row["cid"]]}}, ensure_ascii=False)
        with source.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["cid", "text", "response"], delimiter="\t")
            writer.writeheader(); writer.writerows(rows)
        ready["sourceSha256"] = file_sha256(source)
        manifest = json.loads(Path(ready["manifestPath"]).read_text())
        manifest["sources"][0]["sha256"] = ready["sourceSha256"]
        Path(ready["manifestPath"]).write_text(json.dumps(manifest))
        ready["manifestSha256"] = file_sha256(ready["manifestPath"])
        value = build_addon(ready)
        counts = {item["key"]: item["totalSessions"] for item in value["startPromptDirections"]}
        self.assertTrue(all(count == 0 for count in counts.values()))
        self.assertEqual(value["directionCoverage"]["unlabelledSessions"], len(samples))
        self.assertEqual(value["directionCoverage"]["unsupportedPrefixSessions"], 0)

        rows[0]["response"] = json.dumps({"extra": {"opening_prompt": "未知新格式==【system】\n高频兴趣"}}, ensure_ascii=False)
        with source.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["cid", "text", "response"], delimiter="\t")
            writer.writeheader(); writer.writerows(rows)
        ready["sourceSha256"] = file_sha256(source)
        manifest["sources"][0]["sha256"] = ready["sourceSha256"]
        Path(ready["manifestPath"]).write_text(json.dumps(manifest))
        ready["manifestSha256"] = file_sha256(ready["manifestPath"])
        changed = build_addon(ready)
        self.assertEqual(changed["directionCoverage"]["unlabelledSessions"], len(samples) - 1)
        self.assertEqual(changed["directionCoverage"]["unsupportedPrefixSessions"], 1)
        self.assertTrue(all(item["totalSessions"] == 0 for item in changed["startPromptDirections"]))
        self.assertEqual(verify_addon(changed, ready)["status"], "passed")

    def test_sorting_timestamp_created_at_and_source_row(self):
        def reorder(rows):
            rows[0]["timestamp"] = "2"
            rows[1]["timestamp"] = "1"
            rows[2]["timestamp"] = "bad"
            rows[3]["timestamp"] = ""
            rows[2]["created_at"] = "2026-09-13 13:00:00"
            rows[3]["created_at"] = "2026-09-13 12:00:00"
            rows[4]["timestamp"] = rows[5]["timestamp"] = "0"
            rows.reverse()
        ready = self.package({"a": ["other", RULES["placeholder"]], "b": ["other", RULES["legacyExact"][0]], "c": [RULES["placeholder"], "对"]}, mutate=reorder)
        value = build_addon(ready)
        self.assertEqual([g["totalSessions"] for g in value["groups"]], [2, 1, 0])
        self.assertEqual(verify_addon(value, ready)["status"], "passed")

    def test_empty_hints_and_no_samples(self):
        ready = self.package({"a": ["", "对"], "b": [RULES["systemHints"][0]], "c": ["你好"]})
        value = build_addon(ready)
        self.assertEqual(value["diagnostics"], dict(emptyFirstSessions=1, systemHintFirstSessions=1))
        self.assertIsNone(value["groups"][0]["replyRate"])
        self.assertEqual(value["groups"][2]["replySessions"], 1)
        self.assertEqual(verify_addon(value, ready)["status"], "passed")
        empty = self.package({}, "2026-09-12")
        zero = build_addon(empty)
        for group in zero["groups"]:
            for key in ("averageTurns", "replyRate", "sessionShare", "multiTurnRate", "multiTurnAverageTurns", "fivePlusRate"):
                self.assertIsNone(group[key])
        self.assertEqual(verify_addon(zero, empty)["status"], "passed")

    def test_cross_day_is_daily_and_unavailable_is_null(self):
        prev = self.package({"same": ["用户首条"]}, "2026-09-12")
        ready = self.package({"same": [RULES["placeholder"], "对"], "new": ["你好"]})
        value = build_addon(ready, prev)
        self.assertEqual(value["groups"][0]["totalSessions"], 1)
        self.assertEqual(value["crossDay"]["overlapSessions"], 1)
        self.assertEqual(verify_addon(value, ready, prev)["status"], "passed")
        unavailable = build_addon(ready)
        self.assertIsNone(unavailable["crossDay"]["overlapSessions"])
        with self.assertRaises(ValueError):
            build_addon(ready, ready)

    def test_template_independent_structure(self):
        ready = self.package({"a": ["a", "b", "c", "d", "e"], "b": ["b"]})
        first = build_addon(ready)
        changed = self.package({"a": [RULES["placeholder"]] * 5, "b": [RULES["legacyExact"][0]]}, "2026-09-12")
        second = build_addon(changed)
        self.assertEqual(first["totals"], second["totals"])

    def test_invalid_rows_rejected(self):
        mutations = [lambda r: r[1].update(id=r[0]["id"]),
                     lambda r: r[1].update(source_row=r[0]["source_row"]),
                     lambda r: r[1].update(clientId="different"),
                     lambda r: r[1].update(cid=""),
                     lambda r: r[1].update(sceneId="904"),
                     lambda r: r[1].update(created_at="2026-09-12 10:00:00"),
                     lambda r: r[1].update(source_row=50),
                     lambda r: r[1].update(timestamp="nan")]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                ready = self.package({"a": ["a", "b"]}, mutate=mutate)
                with self.assertRaises(ValueError):
                    build_addon(ready)

    def test_source_binding(self):
        ready = self.package({"a": ["hi"]})
        bad = dict(ready, detailSha256="0" * 64)
        with self.assertRaises(ValueError):
            build_addon(bad)
        manifest = json.loads(Path(ready["manifestPath"]).read_text())
        manifest["outputs"][0]["file"] = "unrelated.csv"
        Path(ready["manifestPath"]).write_text(json.dumps(manifest))
        ready["manifestSha256"] = file_sha256(ready["manifestPath"])
        with self.assertRaises(ValueError):
            build_addon(ready)

    def test_aggregate_tampering_and_privacy_rejected(self):
        ready = self.package({"a": [RULES["placeholder"], "对"]})
        value = build_addon(ready)
        for mutation in (lambda v: v["groups"][0].update(replySessions=2),
                         lambda v: v["groups"][0].update(replyRate=0),
                         lambda v: v["groups"][1].update(averageTurns=0),
                         lambda v: v.update(cid="private"),
                         lambda v: v["source"].update(text="private"),
                         lambda v: v["directionCoverage"].update(classifiedSessions=2),
                         lambda v: v["crossDay"].update(overlapSessions=0)):
            damaged = copy.deepcopy(value)
            mutation(damaged)
            with self.assertRaises(ValueError):
                validate_opening_cohorts(damaged)
        damaged = copy.deepcopy(value)
        damaged["groups"][0]["replySessions"] = 0
        damaged["groups"][0]["replyRate"] = 0
        damaged["startPromptDirections"][0]["replySessions"] = 0
        damaged["startPromptDirections"][0]["replyRate"] = 0
        validate_opening_cohorts(damaged)
        with self.assertRaisesRegex(ValueError, "independent"):
            verify_addon(damaged, ready)

    def test_detached_package_hashes_and_no_overwrite(self):
        ready = self.package({"a": ["hi", "对"]})
        output = self.root / "addon"
        write_package(ready, None, output)
        manifest = json.loads((output / "opening-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["outputs"]), 2)
        for artifact in manifest["outputs"]:
            self.assertEqual(file_sha256(output / artifact["file"]), artifact["sha256"])
        with self.assertRaisesRegex(ValueError, "refusing to replace"):
            write_package(ready, None, output)


if __name__ == "__main__":
    unittest.main()
