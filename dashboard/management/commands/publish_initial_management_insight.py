from datetime import date

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from dashboard.insights import create_generated_insight, publish_revision, save_revision
from dashboard.models import DailySnapshot, ManagementInsight
from dashboard.product_versions import DEFAULT_PRODUCT_VERSION


EXPECTED_HASHES = {
    "2026-08-28": "2a172ff6ccca249af2f721dc1ccc1a838a0d1c3c70e565378472343c7bead682",
    "2026-08-29": "c20016c5065c92f43e5338983832271e902394f9e9c8fd65a99439b38083bb44",
    "2026-08-30": "5a57a92059dc4031d92ce6ac2c8b36dc5403dd13c5810fc609ad3583eb2c030a",
}


class Command(BaseCommand):
    help = "Publish the verified 2026-08-28 to 2026-08-30 management insight."

    def add_arguments(self, parser):
        parser.add_argument("--author", required=True, help="Existing account email used for the audit trail.")

    def handle(self, *args, **options):
        author_email = options["author"].strip().lower()
        author = get_user_model().objects.filter(email__iexact=author_email).first()
        if author is None:
            raise CommandError(f"Author account does not exist: {author_email}")

        actual = {
            item.data_date.isoformat(): item.workbook_sha256
            for item in DailySnapshot.objects.filter(
                product_version=DEFAULT_PRODUCT_VERSION,
                data_date__range=("2026-08-28", "2026-08-30"),
            )
        }
        if actual != EXPECTED_HASHES:
            raise CommandError("Published snapshot hashes do not match the verified 8.28-8.30 packages.")

        existing = ManagementInsight.objects.filter(
            product_version=DEFAULT_PRODUCT_VERSION,
            start_date="2026-08-28",
            end_date="2026-08-30",
            origin=ManagementInsight.Origin.INITIAL,
        ).first()
        if existing:
            if existing.status != ManagementInsight.Status.PUBLISHED:
                publish_revision(existing.id, existing.current_revision, author)
            self.stdout.write(self.style.SUCCESS(f"Initial insight already exists: {existing.id}"))
            return

        insight = create_generated_insight(
            date(2026, 8, 28),
            date(2026, 8, 30),
            author,
            origin=ManagementInsight.Origin.INITIAL,
            product_version=DEFAULT_PRODUCT_VERSION,
        )
        revision = insight.revisions.get(revision=1)
        facts = [*revision.facts]
        facts.extend(
            [
                {
                    "label": "深度变化贡献",
                    "value": "86.81%",
                    "note": "对称分解显示平均会话深度减少贡献 -94,098 轮，占总轮次减少 108,395 轮的 86.81%。",
                    "status": "confirmed",
                },
                {
                    "label": "20轮以上深会话",
                    "value": "贡献 83.65%",
                    "note": "20轮以上会话承载的轮次减少 90,678 轮，占总轮次减少 108,395 轮的 83.65%。",
                    "status": "confirmed",
                },
                {
                    "label": "20时异常窗口",
                    "value": "贡献 28.35%",
                    "note": "8月30日20:00至20:59较8月29日少17,275轮，占当日总降幅60,938轮的28.35%。",
                    "status": "association",
                },
            ]
        )
        insight = save_revision(
            insight.id,
            {
                "baseRevision": 1,
                "title": "8月28日至30日总轮次下降专项说明",
                "summary": "8月28日至30日总轮次下降21.27%。会话数仅下降3.10%，平均会话深度变化解释86.81%的轮次降幅；开口率和真实参与率同步走弱，20轮以上深会话减少是最集中的结构变化。以上结论描述数据机制和观察性关联，不将服务、日期或运营因素写成已确认原因。",
                "facts": facts,
                "mechanisms": [
                    "总轮次从509,676轮降至401,281轮。对称分解显示平均会话深度贡献-94,098轮，会话数贡献-14,297轮，深度变化解释86.81%的总降幅。",
                    "20轮以上会话承载的轮次从324,968轮降至234,290轮，减少90,678轮，占总轮次降幅的83.65%，说明下降集中在深会话尾部。",
                ],
                "associations": [
                    "开口率下降4.55个百分点，真实参与率下降4.43个百分点，与会话深度走弱同步发生；同步变化本身不能证明因果。",
                    "8月30日20:00至20:59较前一日少17,275轮，解释当日总轮次降幅的28.35%，属于优先排查的异常窗口。",
                ],
                "hypotheses": [
                    "待验证：20时段是否存在服务可用性、延迟或设备在线异常。",
                    "待验证：自然日期和周末结构是否改变了高深度用户的使用时长。",
                    "待验证：推送、内容供给或运营节奏是否影响开口与持续参与。",
                ],
                "actions": [
                    "优先核对8月30日20时段的服务日志、错误率、延迟和在线设备量。",
                    "按20轮以上会话继续拆分用户、时段与意图结构，确认深会话减少集中在哪些群体。",
                    "后续三天持续观察开口率、真实参与率、三层平均轮数和20轮以上轮次贡献是否恢复。",
                ],
            },
            author,
        )
        publish_revision(insight.id, insight.current_revision, author)
        self.stdout.write(self.style.SUCCESS(f"Published initial management insight: {insight.id}"))
