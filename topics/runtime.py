import csv
import hashlib
import json
import os
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

from django.conf import settings

from publisher.date_contract import CsvDateContractError, validate_csv_data_date

from .models import TopicJob
from .storage import job_directory, safe_job_path, upload_path


REQUIRED_COLUMNS = {
    "id",
    "clientId",
    "cid",
    "text",
    "response",
    "timestamp",
    "intention",
    "subIntention",
    "created_at",
    "sceneId",
    "model",
}


class TopicRuntimeError(RuntimeError):
    code = "analysis_failed"


class TopicInputError(TopicRuntimeError):
    code = "invalid_input"


class TopicConfigurationError(TopicRuntimeError):
    code = "configuration_error"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _last_json(output: str, required_key: str | None = None) -> dict:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and (required_key is None or required_key in value):
            return value
    raise TopicRuntimeError("分析工具未返回预期的结构化结果。")


def _copy_runtime(source: Path, destination: Path) -> None:
    if destination.exists():
        return
    if not source.is_dir():
        raise TopicConfigurationError("分析运行时目录未配置或不存在。")
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("scripts", "references", "vendor"):
        child = source / name
        if not child.is_dir():
            raise TopicConfigurationError(f"分析运行时缺少 {name}。")
        shutil.copytree(child, destination / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _safe_environment(*, candidate_model: bool = False) -> dict[str, str]:
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "NODE_EXTRA_CA_CERTS",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed and value}
    env["TEENI_ANALYSIS_PYTHON"] = settings.TEENI_ANALYSIS_PYTHON
    env["TEENI_NODE_MODULES"] = settings.TEENI_NODE_MODULES
    if candidate_model:
        env["TEENI_TOPICS_BASE_URL"] = settings.TEENI_TOPICS_BASE_URL
        if settings.TEENI_TOPICS_CA_FILE:
            env["SSL_CERT_FILE"] = settings.TEENI_TOPICS_CA_FILE
    return env


def read_secret_file(path: Path) -> str:
    if not path.is_file():
        raise TopicConfigurationError("模型密钥文件未配置。")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise TopicConfigurationError("模型密钥文件权限必须为600。")
    value = path.read_text(encoding="utf-8").strip()
    if not value or "\n" in value or "\r" in value:
        raise TopicConfigurationError("模型密钥文件格式无效。")
    return value


class AnalysisRuntime:
    def __init__(self, runner=None):
        self.runner = runner or subprocess.run

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: int,
        label: str,
    ) -> str:
        try:
            result = self.runner(
                command,
                cwd=cwd,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            error = TopicRuntimeError(f"{label}超时。")
            error.code = "analysis_timeout"
            raise error from exc
        except OSError as exc:
            raise TopicConfigurationError(f"无法启动{label}。") from exc

        if result.returncode != 0:
            raise TopicRuntimeError(f"{label}失败，退出码 {result.returncode}。")
        return result.stdout

    def _prepare(self, job: TopicJob) -> Path:
        destination = safe_job_path(job.id, "runtime/base", create_parent=True)
        _copy_runtime(Path(settings.TEENI_BASE_SKILL_ROOT), destination)
        return destination

    def _relative(self, job: TopicJob, value: str | Path) -> str:
        base = job_directory(job.id).resolve()
        path = Path(value).resolve()
        try:
            relative = path.relative_to(base)
        except ValueError as exc:
            raise TopicRuntimeError("分析工具返回了任务目录之外的路径。") from exc
        if not path.is_file():
            raise TopicRuntimeError("分析工具返回的产物不存在。")
        return relative.as_posix()

    def validate_upload(self, job: TopicJob) -> dict:
        source = upload_path(job.id)
        if not source.is_file() or source.stat().st_size != job.expected_size:
            raise TopicInputError("上传文件不完整。")
        actual_hash = sha256_file(source)
        if actual_hash != job.expected_sha256:
            error = TopicInputError("上传文件SHA-256与提交值不一致。")
            error.code = "input_hash_mismatch"
            raise error

        csv.field_size_limit(2**31 - 1)
        try:
            with source.open("r", encoding="utf-8-sig", newline="") as stream:
                first_line = stream.readline()
                stream.seek(0)
                delimiter = "\t" if first_line.count("\t") > first_line.count(",") else ","
                reader = csv.reader(stream, delimiter=delimiter)
                header = next(reader, None)
        except (OSError, UnicodeError, csv.Error) as exc:
            raise TopicInputError("CSV无法按UTF-8读取。") from exc
        missing = REQUIRED_COLUMNS - set(header or [])
        if missing:
            raise TopicInputError(f"CSV缺少必需字段：{', '.join(sorted(missing))}")
        try:
            summary = validate_csv_data_date(source, job.data_date, job.scene_id)
        except (OSError, UnicodeError, csv.Error, CsvDateContractError) as exc:
            raise TopicInputError(str(exc)) from exc

        final_path = safe_job_path(job.id, f"input/scene{job.scene_id}.csv", create_parent=True)
        source.replace(final_path)
        return {"inputPath": self._relative(job, final_path), "inputSha256": actual_hash, "rowCount": summary["rowCount"]}

    def run_base(self, job: TopicJob) -> dict:
        runtime = self._prepare(job)
        input_path = safe_job_path(job.id, job.runtime_state["inputPath"])
        output_dir = safe_job_path(job.id, "base", create_parent=True)
        output_dir.mkdir(exist_ok=True)
        output = self._run(
            [
                settings.TEENI_ANALYSIS_NODE,
                str(runtime / "scripts" / "analyze-shared.mjs"),
                "--primary",
                str(input_path),
                "--primary-scene",
                job.scene_id,
                "--primary-label",
                job.product_version,
                "--output-dir",
                str(output_dir),
            ],
            cwd=runtime,
            env=_safe_environment(),
            timeout=8 * 60 * 60,
            label="基础分析",
        )
        result = _last_json(output, "manifestPath")
        return {
            "workbookPath": self._relative(job, result["outputPath"]),
            "primaryDetailPath": self._relative(job, result["primaryDetailPath"]),
            "endingDetailPath": self._relative(job, result["endingDetailPath"]),
            "manifestPath": self._relative(job, result["manifestPath"]),
        }

    def verify_base(self, job: TopicJob) -> None:
        runtime = self._prepare(job)
        state = job.runtime_state["base"]
        render_dir = safe_job_path(job.id, "base-renders", create_parent=True)
        render_dir.mkdir(exist_ok=True)
        self._run(
            [
                settings.TEENI_ANALYSIS_NODE,
                str(runtime / "scripts" / "verify.mjs"),
                "--primary-source",
                str(safe_job_path(job.id, job.runtime_state["inputPath"])),
                "--workbook",
                str(safe_job_path(job.id, state["workbookPath"])),
                "--primary-detail",
                str(safe_job_path(job.id, state["primaryDetailPath"])),
                "--ending-detail",
                str(safe_job_path(job.id, state["endingDetailPath"])),
                "--manifest",
                str(safe_job_path(job.id, state["manifestPath"])),
                "--primary-scene",
                job.scene_id,
                "--render-dir",
                str(render_dir),
            ],
            cwd=runtime,
            env=_safe_environment(),
            timeout=2 * 60 * 60,
            label="基础验证",
        )

    def freeze_interest(self, job: TopicJob) -> dict:
        from interest_engine import ENGINE_VERSION
        from interests.registry import freeze_registry

        source = Path(__file__).resolve().parents[1] / "interest_engine"
        runtime_root = safe_job_path(job.id, f"runtime/interest-engine-{ENGINE_VERSION.rsplit('/', 1)[-1]}", create_parent=True)
        package = runtime_root / "interest_engine"
        if not package.exists():
            runtime_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                source,
                package,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"),
            )
        frozen = freeze_registry(job)
        return {
            "engineVersion": ENGINE_VERSION,
            "runtimePath": runtime_root.resolve().relative_to(job_directory(job.id).resolve()).as_posix(),
            "registryPath": frozen.relative_path,
            "registryVersion": frozen.registry_version,
            "registrySha256": frozen.sha256,
        }

    def _interest_environment(self) -> tuple[dict[str, str], bool]:
        env = _safe_environment(candidate_model=True)
        env["TEENI_TOPIC_MODEL"] = settings.TEENI_TOPIC_MODEL
        key_path = Path(settings.TEENI_TOPICS_API_KEY_FILE)
        if not key_path.is_file():
            return env, False
        try:
            env["TEENI_TOPICS_API_KEY"] = read_secret_file(key_path)
        except TopicConfigurationError:
            return env, False
        return env, True

    def run_interest(self, job: TopicJob) -> dict:
        from interest_engine import ENGINE_VERSION
        from interests.models import InterestSnapshot

        base = job.runtime_state["base"]
        frozen = job.runtime_state["interestRegistry"]
        runtime = safe_job_path(job.id, frozen["runtimePath"])
        registry = safe_job_path(job.id, frozen["registryPath"])
        if not runtime.is_dir() or not registry.is_file() or sha256_file(registry) != frozen["registrySha256"]:
            raise TopicConfigurationError("冻结的兴趣引擎或实体库无效。")
        output_dir = safe_job_path(job.id, "interest", create_parent=True)
        output_dir.mkdir(exist_ok=True)
        history_dir = safe_job_path(job.id, "interest-history", create_parent=True)
        history_dir.mkdir(exist_ok=True)
        command = [
            settings.TEENI_ANALYSIS_PYTHON,
            "-m",
            "interest_engine.cli",
            "analyze",
            "--detail",
            str(safe_job_path(job.id, base["primaryDetailPath"])),
            "--manifest",
            str(safe_job_path(job.id, base["manifestPath"])),
            "--registry",
            str(registry),
            "--output-dir",
            str(output_dir),
            "--data-date",
            str(job.data_date),
        ]
        data_date = date.fromisoformat(str(job.data_date))
        history = list(
            InterestSnapshot.objects.filter(
                product_version=job.product_version,
                data_date__gte=data_date - timedelta(days=7),
                data_date__lt=data_date,
                registry_sha256=frozen["registrySha256"],
                engine_version=frozen["engineVersion"],
            ).order_by("data_date")
        )
        for snapshot in history:
            path = history_dir / f"{snapshot.data_date.isoformat()}.json"
            path.write_text(json.dumps(snapshot.payload, ensure_ascii=False), encoding="utf-8")
            command.extend(["--history", str(path)])
        env, model_enabled = self._interest_environment()
        if model_enabled:
            command.append("--enable-candidate-model")
        output = self._run(
            command,
            cwd=runtime,
            env=env,
            timeout=settings.TEENI_INTEREST_ANALYSIS_TIMEOUT_SECONDS,
            label="本地兴趣分析",
        )
        result = _last_json(output, "manifestPath")
        if not result.get("ok"):
            raise TopicRuntimeError("本地兴趣分析未完成。")
        snapshot_path = output_dir / "teeni-interest-snapshot.json"
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TopicRuntimeError("兴趣分析快照无效。") from exc
        from interests.product_versions import payload_product_version
        if payload_product_version(snapshot) != job.product_version:
            raise TopicRuntimeError("兴趣分析版本与上传版本不一致。")
        if snapshot.get("engineVersion") != ENGINE_VERSION:
            raise TopicRuntimeError("兴趣分析引擎版本不一致。")
        return {
            "engineVersion": ENGINE_VERSION,
            "registryVersion": frozen["registryVersion"],
            "registrySha256": frozen["registrySha256"],
            "workbookPath": self._relative(job, output_dir / "teeni-interest-report.xlsx"),
            "detailPath": self._relative(job, output_dir / "teeni-interest-detail.safe.csv"),
            "privateDetailPath": self._relative(job, output_dir / "teeni-interest-detail.csv"),
            "snapshotPath": self._relative(job, snapshot_path),
            "segmentPath": self._relative(job, output_dir / "teeni-interest-segments.json"),
            "candidatePath": self._relative(job, output_dir / "teeni-interest-candidates.private.json"),
            "manifestPath": self._relative(job, output_dir / "teeni-interest-manifest.json"),
            "inputQueries": int(snapshot.get("totals", {}).get("inputQueries", 0)),
            "candidateCount": len(snapshot.get("candidates", [])),
            "candidateDegraded": bool(snapshot.get("method", {}).get("candidateDegraded")),
            "candidateFailureCode": snapshot.get("method", {}).get("candidateFailureCode"),
        }

    def verify_interest(self, job: TopicJob) -> None:
        frozen = job.runtime_state["interestRegistry"]
        interest = job.runtime_state["interest"]
        runtime = safe_job_path(job.id, frozen["runtimePath"])
        output_dir = safe_job_path(job.id, Path(interest["manifestPath"]).parent.as_posix())
        output = self._run(
            [
                settings.TEENI_ANALYSIS_PYTHON,
                "-m",
                "interest_engine.cli",
                "verify",
                "--output-dir",
                str(output_dir),
                "--registry",
                str(safe_job_path(job.id, frozen["registryPath"])),
                "--base-detail",
                str(safe_job_path(job.id, job.runtime_state["base"]["primaryDetailPath"])),
            ],
            cwd=runtime,
            env=_safe_environment(),
            timeout=min(settings.TEENI_INTEREST_ANALYSIS_TIMEOUT_SECONDS, 30 * 60),
            label="兴趣结果验证",
        )
        result = _last_json(output, "ok")
        if result.get("ok") is not True or result.get("detailRows") != interest.get("inputQueries"):
            raise TopicRuntimeError("兴趣结果独立验证未通过。")

    def run_interest_backfill(
        self,
        job: TopicJob,
        *,
        registry_path: Path,
        registry_version: str,
        registry_sha256: str,
        output_dir: Path,
    ) -> tuple[dict, dict]:
        from interest_engine import ENGINE_VERSION
        from interests.models import InterestSnapshot

        base = job.runtime_state.get("base", {})
        frozen_runtime = job.runtime_state.get("interestRegistry", {})
        runtime_value = frozen_runtime.get("runtimePath")
        if not runtime_value:
            frozen_runtime = self.freeze_interest(job)
            runtime_value = frozen_runtime["runtimePath"]
        runtime = safe_job_path(job.id, runtime_value)
        if not runtime.is_dir() or not registry_path.is_file() or sha256_file(registry_path) != registry_sha256:
            raise TopicConfigurationError("回补使用的兴趣引擎或实体库无效。")
        output_dir.mkdir(parents=True, exist_ok=True)
        history_dir = output_dir / "history"
        history_dir.mkdir(exist_ok=True)
        command = [
            settings.TEENI_ANALYSIS_PYTHON,
            "-m",
            "interest_engine.cli",
            "backfill-entity",
            "--detail",
            str(safe_job_path(job.id, base["primaryDetailPath"])),
            "--manifest",
            str(safe_job_path(job.id, base["manifestPath"])),
            "--registry",
            str(registry_path),
            "--output-dir",
            str(output_dir),
            "--data-date",
            str(job.data_date),
        ]
        data_date = date.fromisoformat(str(job.data_date))
        history = list(
            InterestSnapshot.objects.filter(
                product_version=job.product_version,
                data_date__gte=data_date - timedelta(days=7), data_date__lt=data_date,
                registry_sha256=registry_sha256,
                engine_version=frozen_runtime.get("engineVersion", ENGINE_VERSION),
            ).order_by("data_date")
        )
        for snapshot in history:
            path = history_dir / f"{snapshot.data_date.isoformat()}.json"
            path.write_text(json.dumps(snapshot.payload, ensure_ascii=False), encoding="utf-8")
            command.extend(["--history", str(path)])
        output = self._run(
            command,
            cwd=runtime,
            env=_safe_environment(),
            timeout=settings.TEENI_INTEREST_ANALYSIS_TIMEOUT_SECONDS,
            label="兴趣实体回补",
        )
        result = _last_json(output, "manifestPath")
        if result.get("ok") is not True:
            raise TopicRuntimeError("兴趣实体回补未完成。")
        snapshot_path = output_dir / "teeni-interest-snapshot.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        interest = {
            "engineVersion": ENGINE_VERSION,
            "registryVersion": registry_version,
            "registrySha256": registry_sha256,
            "workbookPath": self._relative(job, output_dir / "teeni-interest-report.xlsx"),
            "detailPath": self._relative(job, output_dir / "teeni-interest-detail.safe.csv"),
            "privateDetailPath": self._relative(job, output_dir / "teeni-interest-detail.csv"),
            "snapshotPath": self._relative(job, snapshot_path),
            "segmentPath": self._relative(job, output_dir / "teeni-interest-segments.json"),
            "candidatePath": self._relative(job, output_dir / "teeni-interest-candidates.private.json"),
            "manifestPath": self._relative(job, output_dir / "teeni-interest-manifest.json"),
            "inputQueries": int(snapshot.get("totals", {}).get("inputQueries", 0)),
            "candidateCount": len(snapshot.get("candidates", [])),
            "candidateDegraded": bool(snapshot.get("method", {}).get("candidateDegraded")),
            "candidateFailureCode": snapshot.get("method", {}).get("candidateFailureCode"),
        }
        frozen = {
            "engineVersion": ENGINE_VERSION,
            "runtimePath": runtime_value,
            "registryPath": self._relative(job, registry_path),
            "registryVersion": registry_version,
            "registrySha256": registry_sha256,
        }
        verify_output = self._run(
            [
                settings.TEENI_ANALYSIS_PYTHON,
                "-m",
                "interest_engine.cli",
                "verify",
                "--output-dir",
                str(output_dir),
                "--registry",
                str(registry_path),
                "--base-detail",
                str(safe_job_path(job.id, base["primaryDetailPath"])),
            ],
            cwd=runtime,
            env=_safe_environment(),
            timeout=min(settings.TEENI_INTEREST_ANALYSIS_TIMEOUT_SECONDS, 30 * 60),
            label="兴趣回补验证",
        )
        verified = _last_json(verify_output, "ok")
        if verified.get("ok") is not True or verified.get("detailRows") != interest["inputQueries"]:
            raise TopicRuntimeError("兴趣回补独立验证未通过。")
        return interest, frozen
