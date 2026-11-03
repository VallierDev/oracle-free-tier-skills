"""Create at most one configured E2 Micro or A1 instance with free-tier guards."""
import argparse
import fcntl
import hashlib
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import uuid

import oci

REGION = DOMAIN = TENANCY = NAME = None
STATE = None
CONFIG = {}
SHAPE = "VM.Standard.E2.1.Micro"
TARGET = "e2-micro"
PRESETS = {
    "e2-micro": ("VM.Standard.E2.1.Micro", 1, 1),
    "a1-2c12g": ("VM.Standard.A1.Flex", 2, 12),
    "a1-4c24g": ("VM.Standard.A1.Flex", 4, 24),
}


def configure(settings_path):
    global CONFIG, REGION, DOMAIN, TENANCY, NAME, STATE, SHAPE, TARGET
    CONFIG = json.loads(Path(settings_path).expanduser().read_text())
    REGION = CONFIG["region"]
    DOMAIN = CONFIG["availability_domain"]
    TENANCY = CONFIG["expected_tenancy"]
    NAME = CONFIG.get("instance_name", "oci-e2-once")
    TARGET = CONFIG.get("target", "e2-micro")
    if TARGET not in PRESETS:
        raise ValueError("target must be e2-micro, a1-2c12g or a1-4c24g")
    SHAPE = PRESETS[TARGET][0]
    if not REGION or not DOMAIN or not TENANCY.startswith("ocid1.tenancy."):
        raise ValueError("complete region, availability_domain and expected_tenancy")
    if not NAME or len(NAME) > 100:
        raise ValueError("invalid instance_name")
    STATE = Path(CONFIG["state_dir"]).expanduser().resolve()
    return CONFIG


MODELS = oci.core.models


def client(cls, config, service):
    suffix = "oraclecloud.com" if cls is oci.core.VirtualNetworkClient else "oci.oraclecloud.com"
    return cls(config, service_endpoint=f"https://{service}.{REGION}.{suffix}",
               timeout=(5, 20), retry_strategy=oci.retry.NoneRetryStrategy())


def save(path, record):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


def a1_usage(cfg, now):
    """Read already-metered A1 consumption, including terminated instances."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    query = oci.usage_api.models.RequestSummarizedUsagesDetails(
        tenant_id=TENANCY, time_usage_started=start, time_usage_ended=end,
        granularity="MONTHLY", query_type="USAGE", group_by=["skuPartNumber", "unit"])
    units = {"B93297": {"OCPU Hours", "OCPU Per Hour", "OCPU HOURS"},
             "B93298": {"GB Hours", "Gigabyte Per Hour", "GB HOURS"}}
    for suffix in ("oci.oraclecloud.com", "oraclecloud.com"):
        api = oci.usage_api.UsageapiClient(
            cfg, service_endpoint=f"https://usageapi.{REGION}.{suffix}",
            timeout=(5, 20), retry_strategy=oci.retry.NoneRetryStrategy())
        totals = {sku: Decimal(0) for sku in units}
        observed_rows = 0
        page, seen = None, set()
        try:
            while True:
                response = api.request_summarized_usages(query, page=page, limit=1000,
                                                        retry_strategy=oci.retry.NoneRetryStrategy())
                observed_rows += len(response.data.items)
                for row in response.data.items:
                    sku = row.sku_part_number
                    if sku not in units:
                        continue
                    if row.unit not in units[sku] or row.computed_quantity is None or row.is_forecast is True:
                        raise ValueError("unknown A1 usage unit, quantity or forecast")
                    value = Decimal(str(row.computed_quantity))
                    if not value.is_finite() or value < 0:
                        raise ValueError("invalid A1 usage quantity")
                    totals[sku] += value
                page = response.headers.get("opc-next-page")
                if not page:
                    if not observed_rows:
                        raise ValueError("current-month usage coverage is unknown")
                    return totals["B93297"], totals["B93298"]
                if page in seen:
                    raise ValueError("repeated A1 usage page cursor")
                seen.add(page)
        except oci.exceptions.ServiceError as exc:
            if exc.status != 401 or suffix == "oraclecloud.com":
                raise


def a1_guard(active, cfg):
    """Check verified monthly entitlement, fleet size and metered prior usage."""
    entitlement = CONFIG.get("a1_free_allowance", {})
    limit = (entitlement.get("ocpu_hours", 1500), entitlement.get("memory_gb_hours", 9000))
    if limit not in {(1500, 9000), (3000, 18000)}:
        raise ValueError("unsupported A1 entitlement; verify the account rate card")
    if limit == (3000, 18000) and entitlement.get("verified_from_account") is not True:
        return {"status": "free_quota_guard", "reason": "verify_legacy_a1_allowance"}
    _, cores, memory = PRESETS[TARGET]
    for instance in active:
        if instance.shape != SHAPE:
            continue
        size = instance.shape_config
        if size is None or size.ocpus is None or size.memory_in_gbs is None:
            raise ValueError("existing A1 shape config is unknown")
        if any(not math.isfinite(value) or value <= 0 for value in [size.ocpus, size.memory_in_gbs]):
            raise ValueError("existing A1 size must be finite and positive")
        cores += size.ocpus
        memory += size.memory_in_gbs
    # 744 hours covers the longest calendar month and keeps the ongoing fleet inside the allowance.
    if cores * 744 > limit[0] or memory * 744 > limit[1]:
        return {"status": "free_quota_guard", "reason": "a1_fleet_exceeds_allowance",
                "fleet_ocpus_after": cores, "fleet_memory_gb_after": memory}
    now = datetime.now(timezone.utc)
    end = (now.replace(day=28, hour=0, minute=0, second=0, microsecond=0)
           + timedelta(days=4)).replace(day=1)
    remaining = Decimal(str((end - now).total_seconds() / 3600))
    used_cores, used_memory = a1_usage(cfg, now)
    # Reserve 24 hours for delayed metering, including recently deleted instances.
    reserve = Decimal(24)
    if (used_cores + Decimal(str(cores)) * remaining + Decimal(limit[0]) / 744 * reserve > limit[0]
            or used_memory + Decimal(str(memory)) * remaining + Decimal(limit[1]) / 744 * reserve > limit[1]):
        return {"status": "free_quota_guard", "reason": "a1_current_month_usage_near_limit"}
    return None


def run(dry_run, preflight_only=False):
    cfg = oci.config.from_file(CONFIG.get("oci_config", "~/.oci/config"), CONFIG["profile"])
    if (cfg["tenancy"], cfg["region"]) != (TENANCY, REGION):
        raise ValueError("wrong account or region")
    compute = client(oci.core.ComputeClient, cfg, "iaas")
    shape_report = MODELS.CreateCapacityReportShapeAvailabilityDetails(instance_shape=SHAPE)
    if TARGET != "e2-micro":
        _, cores, memory = PRESETS[TARGET]
        shape_report.instance_shape_config = MODELS.CapacityReportInstanceShapeConfig(
            ocpus=cores, memory_in_gbs=memory)
    report = MODELS.CreateComputeCapacityReportDetails(
        compartment_id=TENANCY, availability_domain=DOMAIN,
        shape_availabilities=[shape_report])
    state_path = STATE / "launch.json"
    scope = {"tenancy": TENANCY, "region": REGION, "domain": DOMAIN, "name": NAME}
    if TARGET != "e2-micro":
        scope["target"] = TARGET
    binding = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    binding_path = STATE / "scope.json"
    if binding_path.exists():
        if json.loads(binding_path.read_text()).get("binding") != binding:
            raise ValueError("state directory belongs to another account or target")
    else:
        if state_path.exists():
            raise ValueError("existing state has no account binding; reconcile manually")
        save(binding_path, {"binding": binding})
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state.get("instance_id"):
            instance = compute.get_instance(state["instance_id"]).data
            status = "instance_terminal" if instance.lifecycle_state in {"TERMINATED", "TERMINATING"} else "submitted"
            return {"status": status, "instance_state": instance.lifecycle_state}
        instances = oci.pagination.list_call_get_all_results(
            compute.list_instances, TENANCY, display_name=NAME).data
        matches = [i for i in instances if (i.freeform_tags or {}).get("grab-intent") == state["token"]]
        if len(matches) == 1:
            save(state_path, dict(state, instance_id=matches[0].id))
            return {"status": "submitted", "instance_state": matches[0].lifecycle_state}
        return {"status": "unknown_launch_result", "action": "manual reconciliation required"}

    if not preflight_only:
        rows = compute.create_compute_capacity_report(report).data.shape_availabilities
        statuses = [r.availability_status for r in (rows or []) if r.instance_shape == SHAPE]
        if not statuses:
            return {"status": "capacity_unknown"}
        if "AVAILABLE" not in statuses:
            return {"status": "capacity_check", "availability": statuses}
        if dry_run:
            return {"status": "capacity_available"}

    identity = client(oci.identity.IdentityClient, cfg, "identity")
    storage = client(oci.core.BlockstorageClient, cfg, "iaas")
    network = client(oci.core.VirtualNetworkClient, cfg, "iaas")
    def all_rows(fn, *args, **kwargs):
        return oci.pagination.list_call_get_all_results(fn, *args, **kwargs).data
    regions = identity.list_region_subscriptions(TENANCY).data
    if not any(r.region_name == REGION and r.is_home_region for r in regions):
        raise ValueError("selected region must be the home region")
    compartments = [TENANCY] + [
        c.id for c in all_rows(identity.list_compartments, TENANCY,
                               compartment_id_in_subtree=True, access_level="ANY")
        if c.lifecycle_state != "DELETED"]
    domains = identity.list_availability_domains(TENANCY).data
    instances = []
    total_gb = 0
    for compartment in compartments:
        instances.extend(all_rows(compute.list_instances, compartment))
        volumes = all_rows(storage.list_volumes, compartment_id=compartment)
        for domain in domains:
            volumes.extend(all_rows(storage.list_boot_volumes, compartment_id=compartment,
                                    availability_domain=domain.name))
        total_gb += sum(v.size_in_gbs for v in volumes if v.lifecycle_state != "TERMINATED")
    active = [i for i in instances if i.lifecycle_state != "TERMINATED"]
    if any(i.display_name == NAME for i in active):
        return {"status": "instance_already_exists"}
    micros = [i for i in active if i.shape == "VM.Standard.E2.1.Micro"]
    if (TARGET == "e2-micro" and len(micros) >= 2) or total_gb + 50 > 200:
        return {"status": "free_quota_guard", "existing_micros": len(micros), "storage_gb": total_gb}
    if TARGET != "e2-micro":
        guard = a1_guard(active, cfg)
        if guard:
            return guard
    image_client = oci.core.ComputeClient(
        cfg, service_endpoint=f"https://iaas.{REGION}.oraclecloud.com",
        timeout=(5, 20), retry_strategy=oci.retry.NoneRetryStrategy())
    images = image_client.list_images(
        TENANCY, shape=SHAPE, operating_system="Canonical Ubuntu",
        operating_system_version="24.04", sort_by="TIMECREATED", sort_order="DESC", limit=10).data
    image = next((item for item in images
                  if not item.compartment_id and item.listing_type is None
                  and item.lifecycle_state == "AVAILABLE"
                  and item.display_name.startswith("Canonical-Ubuntu-24.04-")
                  and (("aarch64" in item.display_name) == (TARGET != "e2-micro"))), None)
    if image is None:
        raise ValueError("no eligible public Ubuntu 24.04 platform image")
    subnet = CONFIG["subnet_id"]
    subnet_info = network.get_subnet(subnet).data
    vcn = network.get_vcn(subnet_info.vcn_id).data
    if (subnet_info.prohibit_public_ip_on_vnic or vcn.compartment_id != TENANCY
            or subnet_info.compartment_id != TENANCY
            or (subnet_info.availability_domain and subnet_info.availability_domain != DOMAIN)):
        raise ValueError("expected a public subnet in the root compartment and target domain")
    key_path = Path(CONFIG["ssh_public_key"]).expanduser()
    key = key_path.read_text().strip()
    if not key.startswith("ssh-ed25519 "):
        raise ValueError("expected local SSH public key")
    if preflight_only:
        return {"status": "preflight_ready", "region": REGION, "shape": SHAPE,
                "existing_micros": len(micros), "storage_gb": total_gb,
                "boot_gb": 50, "boot_vpus": 10, "image": image.display_name}
    token = uuid.uuid4().hex
    details = MODELS.LaunchInstanceDetails(
        availability_domain=DOMAIN, compartment_id=TENANCY, display_name=NAME, shape=SHAPE,
        source_details=MODELS.InstanceSourceViaImageDetails(
            source_type="image", image_id=image.id,
            boot_volume_size_in_gbs=50, boot_volume_vpus_per_gb=10),
        create_vnic_details=MODELS.CreateVnicDetails(subnet_id=subnet, assign_public_ip=True),
        metadata={"ssh_authorized_keys": key},
        freeform_tags={"grab-intent": token})
    if TARGET != "e2-micro":
        _, cores, memory = PRESETS[TARGET]
        details.shape_config = MODELS.LaunchInstanceShapeConfigDetails(ocpus=cores, memory_in_gbs=memory)
    save(state_path, {"token": token})
    try:
        instance = compute.launch_instance(details, opc_retry_token=token,
                                           retry_strategy=oci.retry.NoneRetryStrategy()).data
    except oci.exceptions.ServiceError as exc:
        if exc.status == 500 and "out of host capacity" in exc.message.lower():
            state_path.unlink()
            return {"status": "capacity_lost"}
        raise
    save(state_path, {"token": token, "instance_id": instance.id})
    return {"status": "submitted", "instance_state": instance.lifecycle_state}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True, help="local grab configuration JSON")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--launch", action="store_true", help="allow creation of one instance")
    mode.add_argument("--preflight", action="store_true", help="validate launch inputs without submitting")
    args = parser.parse_args()
    os.umask(0o077)
    configure(args.settings)
    STATE.mkdir(mode=0o700, exist_ok=True)
    with (STATE / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "another_run_active"}))
            return 0
        try:
            result = run(dry_run=not args.launch, preflight_only=args.preflight)
        except oci.exceptions.ServiceError as exc:
            result = {"status": "api_error", "http": exc.status,
                      "code": exc.code, "operation": exc.operation_name}
        except Exception as exc:
            result = {"status": "error", "type": type(exc).__name__}
    print(json.dumps(result), flush=True)
    return 2 if result["status"] in ("api_error", "error", "unknown_launch_result", "capacity_unknown") else 0


if __name__ == "__main__":
    sys.exit(main())
