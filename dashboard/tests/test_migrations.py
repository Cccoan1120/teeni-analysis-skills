from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ProductVersionMigrationTests(TransactionTestCase):
    migrate_from = ("dashboard", "0003_managementinsight_managementinsightrevision")
    migrate_to = ("dashboard", "0004_product_versions")

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_from])
        old_apps = self.executor.loader.project_state([self.migrate_from]).apps
        DailySnapshot = old_apps.get_model("dashboard", "DailySnapshot")
        ManagementInsight = old_apps.get_model("dashboard", "ManagementInsight")
        self.snapshot_id = DailySnapshot.objects.create(
            data_date="2026-08-31",
            scene_id="488",
            core_version="2.5.0",
            rules_version="11.0.0",
            contract_version="teeni-base-detail/1.2.0",
            source_sha256="a" * 64,
            workbook_sha256="b" * 64,
            payload={"schemaVersion": "teeni-dashboard-snapshot/1.1.0"},
        ).id
        self.insight_id = ManagementInsight.objects.create(
            start_date="2026-08-31",
            end_date="2026-08-31",
        ).id
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_to])
        self.apps = self.executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_rows_are_backfilled_and_same_date_m2_can_coexist(self):
        DailySnapshot = self.apps.get_model("dashboard", "DailySnapshot")
        ManagementInsight = self.apps.get_model("dashboard", "ManagementInsight")

        self.assertEqual(DailySnapshot.objects.get(id=self.snapshot_id).product_version, "M1")
        self.assertEqual(ManagementInsight.objects.get(id=self.insight_id).product_version, "M1")
        DailySnapshot.objects.create(
            data_date="2026-08-31",
            product_version="M2",
            scene_id="904",
            core_version="2.5.0",
            rules_version="12.0.0",
            contract_version="teeni-base-detail/1.2.0",
            source_sha256="c" * 64,
            workbook_sha256="d" * 64,
            payload={"schemaVersion": "teeni-dashboard-snapshot/1.2.0"},
        )
        self.assertEqual(DailySnapshot.objects.filter(data_date="2026-08-31").count(), 2)

    def test_m1_only_data_can_reverse_to_previous_schema(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_from])
        old_apps = self.executor.loader.project_state([self.migrate_from]).apps
        DailySnapshot = old_apps.get_model("dashboard", "DailySnapshot")

        row = DailySnapshot.objects.get(id=self.snapshot_id)
        self.assertEqual(row.scene_id, "488")
        self.assertFalse(hasattr(row, "product_version"))
