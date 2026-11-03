#!/usr/bin/env python3
"""Report monthly public egress from OCI billing usage, with an 8 TB marker."""
import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys

import oci

EGRESS_SKUS = {"B88327", "B93455", "B93456"}
GB_UNITS = {"GB", "GB Months"}


def summarize(rows):
    outbound = [row for row in rows if row.sku_part_number in EGRESS_SKUS]
    if not outbound:
        raise ValueError("no egress usage rows; coverage is unknown")
    total = Decimal(0)
    for row in outbound:
        if (row.unit not in GB_UNITS or row.computed_quantity is None
                or row.is_forecast is True):
            raise ValueError("egress quantity, unit or forecast flag is invalid")
        quantity = Decimal(str(row.computed_quantity))
        if not quantity.is_finite() or quantity < 0:
            raise ValueError("egress quantity must be finite and nonnegative")
        total += quantity
    return total


def read_account(account, start, end, config_file):
    cfg = oci.config.from_file(config_file, account["profile"])
    query = oci.usage_api.models.RequestSummarizedUsagesDetails(
        tenant_id=cfg["tenancy"], time_usage_started=start.isoformat() + "T00:00:00Z",
        time_usage_ended=end.isoformat() + "T00:00:00Z", granularity="MONTHLY",
        query_type="USAGE", group_by=["skuPartNumber", "skuName", "unit"],
    )
    for suffix in ("oci.oraclecloud.com", "oraclecloud.com"):
        api = oci.usage_api.UsageapiClient(
            cfg, timeout=(5, 20), retry_strategy=oci.retry.NoneRetryStrategy(),
            service_endpoint=f"https://usageapi.{cfg['region']}.{suffix}",
        )
        rows, page, seen = [], None, set()
        try:
            while True:
                response = api.request_summarized_usages(
                    query, page=page, limit=1000, retry_strategy=oci.retry.NoneRetryStrategy())
                rows.extend(response.data.items)
                page = response.headers.get("opc-next-page")
                if not page:
                    return summarize(rows)
                if page in seen:
                    raise ValueError("repeated pagination cursor")
                seen.add(page)
        except oci.exceptions.ServiceError as exc:
            if exc.status != 401 or suffix == "oraclecloud.com":
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", required=True, help="local accounts JSON")
    parser.add_argument("--oci-config", default="~/.oci/config")
    parser.add_argument("--month", help="UTC month YYYY-MM, default current month")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    today = datetime.now(timezone.utc).date()
    try:
        start = date.fromisoformat(args.month + "-01") if args.month else today.replace(day=1)
        if start > today:
            raise ValueError("month is in the future")
        accounts = json.loads(Path(args.accounts).expanduser().read_text())["accounts"]
        if not accounts or any(not row.get("label") or not row.get("profile") for row in accounts):
            raise ValueError("provide nonempty labels and profile names")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    results = []
    for account in accounts:
        row = {"label": account["label"], "profile": account["profile"], "status": "unknown"}
        try:
            gb = read_account(account, start, end, args.oci_config)
            row.update(status="ok", gb=str(gb), tb=str(gb / 1000), reached_8tb=gb >= 8000)
        except oci.exceptions.ServiceError as exc:
            row.update(error=exc.code, http_status=exc.status)
        except Exception as exc:
            row.update(error=type(exc).__name__)
        results.append(row)
    report = {"month_utc": start.strftime("%Y-%m"),
              "queried_at": datetime.now(timezone.utc).isoformat(),
              "usage_note": "Oracle billing usage may lag up to 24 hours.", "accounts": results}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"{report['month_utc']} Oracle 公网出站用量（UTC 计费月）")
        for row in results:
            if row["status"] == "ok":
                marker = "，已达到 8 TB" if row["reached_8tb"] else ""
                print(f"{row['label']} {Decimal(row['tb']):.3f} TB（{Decimal(row['gb']):.2f} GB{marker}）")
            else:
                print(f"{row['label']} 查询未知（{row['error']}）")
        print(f"查询时间 {report['queried_at']}，Oracle 用量可能延迟最多 24 小时。")
    return 0 if all(row["status"] == "ok" for row in results) else 2


if __name__ == "__main__":
    sys.exit(main())
