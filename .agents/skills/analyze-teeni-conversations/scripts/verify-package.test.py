import csv
import copy
import json
import importlib.util
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

spec = importlib.util.spec_from_file_location("verify_package", Path(__file__).with_name("verify-package.py"))
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class PackageTests(unittest.TestCase):
    def test_structure_reconciliation_rejects_tampering_and_duplicate_ids(self):
        from teeni_analysis_core.session_structure import build_session_structure
        with tempfile.TemporaryDirectory() as folder:
            detail = Path(folder) / "detail.csv"
            rows = [{"id": str(i), "clientId": "anonymous", "cid": "single" if i == 0 else "multi"} for i in range(4)]
            def write(values):
                with detail.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=["id", "clientId", "cid"])
                    writer.writeheader()
                    writer.writerows(values)
            write(rows)
            metrics = build_session_structure(rows)
            audit.verify_structure(detail, metrics)
            with self.assertRaisesRegex(ValueError, "metrics mismatch"):
                audit.verify_structure(detail, {**metrics, "multiTurnRate": .99})
            write(rows + [rows[0]])
            with self.assertRaisesRegex(ValueError, "duplicate identity"):
                audit.verify_structure(detail, metrics)
            write([rows[0], rows[1], {**rows[2], "clientId": "other"}])
            with self.assertRaisesRegex(ValueError, "cross-user cid"):
                audit.verify_structure(detail, metrics)

    def test_supplemental_reconciliation_rejects_tampered_metrics(self):
        from teeni_analysis_core.reporting import build_report_model
        from teeni_analysis_core.rules import load_rules
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, detail = root / "source.csv", root / "detail.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=sorted(audit.REQUIRED))
                writer.writeheader()
                for index, query in enumerate(["和我打招呼并称呼我的名字", "普通问题", "继续聊一下"]):
                    writer.writerow({"id": str(index), "clientId": "anonymous", "cid": "session", "sceneId": "488",
                                     "model": "fixture", "text": query, "timestamp": 1700000000 + index,
                                     "created_at": "2023-11-15 06:13:20", "intention": "", "subIntention": "",
                                     "response": json.dumps({"generated_text": "" if index == 1 else f"普通回复{index}"})})
            report = build_report_model(primary=source, primary_scene="488", primary_detail=detail,
                                        primary_ending_detail=root / "ending.csv")
            metrics = report["primary"]["supplementalMetrics"]
            rules = load_rules()
            audit.verify_supplemental(detail, metrics, rules)
            for keys in (("quality", "responseMissing", "rate"), ("openingFunnel", "realUserReplies"),
                         ("fallbackReopen", "matureSessionReopenRate"), ("inputQuality", "duplicateRecordIds")):
                tampered = copy.deepcopy(metrics)
                target = tampered
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = 999
                with self.subTest(keys=keys), self.assertRaises(ValueError):
                    audit.verify_supplemental(detail, tampered, rules)

    def test_source_identity_scene_counts_and_encoding(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.csv"
            detail = Path(folder) / "detail.csv"
            row = {key: "" for key in audit.REQUIRED}
            row.update(id="1", clientId="anonymous", cid="conversation", sceneId="488", text='Example, "quoted"\n中文', timestamp="1", created_at="2026-01-01")
            for encoding, delimiter in [("utf-8-sig", ","), ("gb18030", "\t")]:
                with source.open("w", encoding=encoding, newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(row), delimiter=delimiter)
                    writer.writeheader()
                    writer.writerow(row)
                values = {key: row[key] for key in audit.IDENTITY}
                values["source_row"] = "2"
                values["ai_text"] = ""
                values["parse_status"] = "解析失败"

                def write_detail(records):
                    with detail.open("w", encoding="utf-8-sig", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=list(values))
                        writer.writeheader()
                        writer.writerows(records)

                write_detail([values])
                self.assertEqual(audit.reconcile(source, detail, "488"), 1)
                with self.assertRaisesRegex(ValueError, "scene mismatch"):
                    audit.reconcile(source, detail, "904")
                write_detail([{**values, "text": "changed"}])
                with self.assertRaisesRegex(ValueError, "text mismatch"):
                    audit.reconcile(source, detail, "488")
                write_detail([])
                with self.assertRaisesRegex(ValueError, "row count mismatch"):
                    audit.reconcile(source, detail, "488")

    def test_real_error_type_not_literal_text(self):
        with tempfile.TemporaryDirectory() as folder:
            workbook = Path(folder) / "test.xlsx"
            for content, expected in [
                ('<c r="A1" t="inlineStr"><is><t>What does #N/A mean?</t></is></c>', 0),
                ('<c r="A1" t="str"><f>"#N/A"</f><v>#N/A</v></c>', 0),
                ('<c r="A1" t="e"><f>1/0</f><v>#DIV/0!</v></c>', 1),
                ('<c r="A1" t="e"><v>#N/A</v></c>', 1),
            ]:
                with ZipFile(workbook, "w") as archive:
                    archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row>{content}</row></sheetData></worksheet>')
                self.assertEqual(len(audit.formula_errors(workbook)), expected)


if __name__ == "__main__":
    unittest.main()
