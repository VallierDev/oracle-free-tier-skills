from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace as S
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import grab
import traffic


class GrabTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.key = self.root / "launch.pub"
        self.key.write_text("ssh-ed25519 AAAAtest-only")
        self.settings = {
            "profile": "TEST", "expected_tenancy": "ocid1.tenancy.test",
            "region": "us-example-1", "availability_domain": "prefix:AD-1",
            "subnet_id": "subnet-test", "ssh_public_key": str(self.key),
            "instance_name": "test-e2-once", "state_dir": str(self.root / "state"),
        }
        self.config = self.root / "settings.json"
        self.config.write_text(json.dumps(self.settings))
        grab.configure(self.config)
        grab.STATE.mkdir()
        self.api = Mock()
        self.api.create_compute_capacity_report.return_value.data = S(shape_availabilities=[
            S(instance_shape=grab.SHAPE, availability_status="AVAILABLE")])
        self.api.list_region_subscriptions.return_value.data = [
            S(region_name=grab.REGION, is_home_region=True)]
        self.api.list_compartments.return_value.data = []
        self.api.list_availability_domains.return_value.data = [S(name=grab.DOMAIN)]
        self.api.list_instances.return_value.data = [S(
            id="existing", display_name="existing", shape=grab.SHAPE,
            lifecycle_state="RUNNING", freeform_tags={})]
        self.api.list_volumes.return_value.data = []
        self.api.list_boot_volumes.return_value.data = [S(size_in_gbs=150, lifecycle_state="AVAILABLE")]
        self.api.list_images.return_value.data = [S(id="image-test", compartment_id=None,
            listing_type=None, lifecycle_state="AVAILABLE", display_name="Canonical-Ubuntu-24.04-test")]
        self.api.get_subnet.return_value.data = S(
            prohibit_public_ip_on_vnic=False, vcn_id="vcn", compartment_id=grab.TENANCY,
            availability_domain=None)
        self.api.get_vcn.return_value.data = S(compartment_id=grab.TENANCY)
        self.api.launch_instance.return_value.data = S(id="created", lifecycle_state="PROVISIONING")
        self.api.get_instance.return_value.data = S(lifecycle_state="RUNNING")
        for patcher in [
            patch.object(grab, "client", return_value=self.api),
            patch.object(grab.oci.core, "ComputeClient", return_value=self.api),
            patch.object(grab.oci.config, "from_file", return_value={"tenancy": grab.TENANCY, "region": grab.REGION}),
            patch.object(grab.oci.pagination, "list_call_get_all_results", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_preflight_is_read_only(self):
        self.assertEqual(grab.run(True, True)["status"], "preflight_ready")
        self.api.launch_instance.assert_not_called()

    def test_storage_overage_blocks(self):
        self.api.list_boot_volumes.return_value.data[0].size_in_gbs = 151
        self.assertEqual(grab.run(False)["status"], "free_quota_guard")
        self.api.launch_instance.assert_not_called()

    def test_single_submit_and_running_recheck(self):
        self.assertEqual(grab.run(False)["status"], "submitted")
        details = self.api.launch_instance.call_args.args[0]
        self.assertEqual(details.shape, grab.SHAPE)
        self.assertEqual(details.source_details.boot_volume_size_in_gbs, 50)
        self.assertEqual(details.source_details.boot_volume_vpus_per_gb, 10)
        self.assertEqual(grab.run(False)["instance_state"], "RUNNING")
        self.assertEqual(self.api.launch_instance.call_count, 1)

    def test_timeout_never_blindly_relaunches(self):
        self.api.launch_instance.side_effect = TimeoutError("test transport failure")
        with self.assertRaises(TimeoutError):
            grab.run(False)
        self.assertEqual(grab.run(False)["status"], "unknown_launch_result")
        self.assertEqual(self.api.launch_instance.call_count, 1)

    def test_state_binding_blocks_changed_account(self):
        grab.run(True, True)
        grab.TENANCY = "ocid1.tenancy.other-test"
        with self.assertRaises(ValueError):
            grab.run(False)
        self.api.launch_instance.assert_not_called()

    def test_wrong_home_region_blocks(self):
        self.api.list_region_subscriptions.return_value.data[0].is_home_region = False
        with self.assertRaises(ValueError):
            grab.run(False)
        self.api.launch_instance.assert_not_called()

    def enable_arm(self, target="a1-2c12g", legacy=False):
        self.settings["target"] = target
        if legacy:
            self.settings["a1_free_allowance"] = {
                "ocpu_hours": 3000, "memory_gb_hours": 18000, "verified_from_account": True}
        self.config.write_text(json.dumps(self.settings))
        grab.configure(self.config)
        self.api.create_compute_capacity_report.return_value.data.shape_availabilities[0].instance_shape = grab.SHAPE
        self.api.list_images.return_value.data[0].display_name = "Canonical-Ubuntu-24.04-aarch64-test"
        patcher = patch.object(grab, "a1_usage", return_value=(Decimal(0), Decimal(0)))
        patcher.start()
        self.addCleanup(patcher.stop)
        clock = patch.object(grab, "datetime")
        clock.start().now.return_value = datetime(2026, 11, 3, tzinfo=timezone.utc)
        self.addCleanup(clock.stop)

    def test_arm_2c12g_report_and_launch(self):
        self.enable_arm()
        self.assertEqual(grab.run(False)["status"], "submitted")
        details = self.api.launch_instance.call_args.args[0]
        report = self.api.create_compute_capacity_report.call_args.args[0]
        self.assertEqual(details.shape, "VM.Standard.A1.Flex")
        self.assertEqual(details.shape_config.ocpus, 2)
        self.assertEqual(details.shape_config.memory_in_gbs, 12)
        self.assertEqual(report.shape_availabilities[0].instance_shape_config.ocpus, 2)
        self.assertIn("aarch64", self.api.list_images.return_value.data[0].display_name)

    def test_arm_4c24g_verified_legacy(self):
        self.enable_arm("a1-4c24g", legacy=True)
        self.assertEqual(grab.run(False)["status"], "submitted")
        details = self.api.launch_instance.call_args.args[0]
        self.assertEqual(details.shape_config.ocpus, 4)
        self.assertEqual(details.shape_config.memory_in_gbs, 24)

    def test_arm_4c24g_default_allowance_blocks(self):
        self.enable_arm("a1-4c24g")
        self.assertEqual(grab.run(False)["reason"], "a1_fleet_exceeds_allowance")
        self.api.launch_instance.assert_not_called()

    def test_arm_legacy_requires_account_verification(self):
        self.enable_arm("a1-4c24g", legacy=True)
        grab.CONFIG["a1_free_allowance"]["verified_from_account"] = False
        self.assertEqual(grab.run(False)["reason"], "verify_legacy_a1_allowance")
        self.api.launch_instance.assert_not_called()

    def test_arm_existing_fleet_counts(self):
        self.enable_arm()
        self.api.list_instances.return_value.data.append(S(
            display_name="existing-arm", shape="VM.Standard.A1.Flex", lifecycle_state="RUNNING",
            shape_config=S(ocpus=1, memory_in_gbs=6)))
        self.assertEqual(grab.run(False)["reason"], "a1_fleet_exceeds_allowance")
        self.api.launch_instance.assert_not_called()

    def test_arm_prior_month_usage_blocks(self):
        self.enable_arm()
        with patch.object(grab, "a1_usage", return_value=(Decimal(1500), Decimal(9000))):
            self.assertEqual(grab.run(False)["reason"], "a1_current_month_usage_near_limit")
        self.api.launch_instance.assert_not_called()

    def test_arm_rejects_x86_image(self):
        self.enable_arm()
        self.api.list_images.return_value.data[0].display_name = "Canonical-Ubuntu-24.04-x86-test"
        with self.assertRaises(ValueError):
            grab.run(False)
        self.api.launch_instance.assert_not_called()

    def test_change_specification_invalidates_state(self):
        grab.run(True, True)
        self.enable_arm()
        with self.assertRaises(ValueError):
            grab.run(False)
        self.api.launch_instance.assert_not_called()


class TrafficTests(unittest.TestCase):
    def row(self, value, sku="B88327", unit="GB Months"):
        return S(sku_part_number=sku, unit=unit, computed_quantity=value, is_forecast=False)

    def test_bill_quantity_ignores_other_services(self):
        self.assertEqual(traffic.summarize([
            self.row(4000), self.row(4000.25, "B93455"), self.row(9999, "CPU-EXAMPLE")
        ]), Decimal("8000.25"))

    def test_missing_units_and_invalid_values_are_unknown(self):
        for rows in [[], [self.row(1, unit="Bytes")], [self.row(None)], [self.row(-1)], [self.row(float("nan"))]]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                traffic.summarize(rows)

    def test_all_pages_read(self):
        api = Mock()
        api.request_summarized_usages.side_effect = [
            S(data=S(items=[self.row(4000)]), headers={"opc-next-page": "next-page"}),
            S(data=S(items=[self.row(4001)]), headers={}),
        ]
        with patch.object(traffic.oci.config, "from_file", return_value={"region": "us-example-1", "tenancy": "test"}), \
             patch.object(traffic.oci.usage_api, "UsageapiClient", return_value=api):
            total = traffic.read_account({"profile": "TEST"}, date(2026, 10, 1), date(2026, 11, 1), "test-config")
        self.assertEqual(total, 8001)
        self.assertEqual(api.request_summarized_usages.call_count, 2)


if __name__ == "__main__":
    unittest.main()
