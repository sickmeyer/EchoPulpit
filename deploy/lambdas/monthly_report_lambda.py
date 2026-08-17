"""
Monthly Report Lambda -- runs on an EventBridge schedule (rate: monthly, on
the 4th) and emails a summary of the *previous* calendar month: AWS spend
(Cost Explorer, filtered to the `Project=echopulpit` cost-allocation tag)
and job outcomes (DynamoDB EchoPulpitJobs).

Runs on the 4th, not the 1st, to give Cost Explorer's usual data-processing
lag (up to ~24-48h for the tail end of the prior month) room to settle
before the query -- avoids a report that silently undercounts the last day
or two of the month.

Deployment note: this Lambda's zip only needs this one file -- Cost
Explorer and DynamoDB clients are both plain boto3, which is present in the
default Lambda runtime, unlike the poller Lambda's google-api-python-client
dependency.
"""
import os
from datetime import datetime, timedelta, timezone

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
SENDER = os.environ["SES_SENDER_ADDRESS"]
RECIPIENT = os.environ["NOTIFY_RECIPIENT_ADDRESS"]
TABLE_NAME = os.environ.get("SERMON_JOBS_TABLE", "EchoPulpitJobs")
PROJECT_TAG_VALUE = os.environ.get("COST_PROJECT_TAG", "echopulpit")

# Cost Explorer is only available in us-east-1 regardless of where the rest
# of the stack runs -- a separate client from the region-local DynamoDB one.
_ce = boto3.client("ce", region_name="us-east-1")
_ddb = boto3.resource("dynamodb", region_name=REGION)
_ses = boto3.client("ses", region_name=REGION)


def _month_bounds(anchor: datetime, months_back: int):
    """
    Returns (start, end) as YYYY-MM-DD strings for the calendar month that
    is `months_back` months before anchor's month -- end is exclusive,
    matching Cost Explorer's TimePeriod convention. months_back=1 from any
    day in September returns August's (start, end).
    """
    first_of_anchor_month = anchor.replace(day=1)
    month_end = first_of_anchor_month
    for _ in range(months_back - 1):
        month_end = month_end.replace(day=1)
        prev = month_end - timedelta(days=1)
        month_end = prev.replace(day=1)
    month_start = (month_end - timedelta(days=1)).replace(day=1)
    return month_start.strftime("%Y-%m-%d"), month_end.strftime("%Y-%m-%d")


def _cost_by_service(start: str, end: str) -> tuple:
    """Returns (total_cost, [(service, cost), ...] sorted desc, non-zero only)."""
    resp = _ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        Filter={"Tags": {"Key": "Project", "Values": [PROJECT_TAG_VALUE]}},
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    lines = []
    total = 0.0
    for result in resp.get("ResultsByTime", []):
        for group in result.get("Groups", []):
            service = group["Keys"][0]
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if amount > 0:
                lines.append((service, amount))
            total += amount
    lines.sort(key=lambda x: x[1], reverse=True)
    return total, lines


def _job_stats(start: str, end: str) -> dict:
    """
    Scans EchoPulpitJobs for items whose completed_at falls in [start, end).
    Lexical string comparison on ISO8601 timestamps is safe here even
    though different code paths format the timezone suffix differently
    (bootstrap.sh's fail-fast path writes "...Z", Python's
    utc_now_iso() writes "...+00:00") -- both compare correctly against a
    plain "YYYY-MM-DDT00:00:00" boundary since the differing suffix only
    ever appears after the comparison has already been decided.
    """
    table = _ddb.Table(TABLE_NAME)
    start_bound = f"{start}T00:00:00"
    end_bound = f"{end}T00:00:00"

    items = []
    scan_kwargs = {
        "FilterExpression": "completed_at BETWEEN :s AND :e",
        "ExpressionAttributeValues": {":s": start_bound, ":e": end_bound},
    }
    while True:
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    complete = [i for i in items if i.get("status") == "COMPLETE"]
    failed = [i for i in items if i.get("status") == "FAILED"]
    return {
        "total": len(items),
        "complete": len(complete),
        "failed": len(failed),
        "failed_items": [
            (i.get("video_id", "?"), i.get("title", "") or "(untitled)", i.get("error", "(no error recorded)"))
            for i in failed
        ],
    }


def _format_report(month_label: str, cost_total: float, cost_lines: list,
                    prior_cost_total: float, stats: dict) -> str:
    lines = [
        f"EchoPulpit monthly report -- {month_label}",
        "=" * (26 + len(month_label)),
        "",
        "JOBS",
        f"  Processed:    {stats['total']}",
        f"  Completed:    {stats['complete']}",
        f"  Failed:       {stats['failed']}",
    ]
    if stats["total"] > 0:
        rate = 100.0 * stats["complete"] / stats["total"]
        lines.append(f"  Success rate: {rate:.0f}%")
    if stats["failed_items"]:
        lines.append("")
        lines.append("  Failed jobs:")
        for video_id, title, error in stats["failed_items"]:
            lines.append(f"    - {title} ({video_id}): {error}")

    lines += ["", "COST (Project=echopulpit)", f"  Total: ${cost_total:.2f}"]
    if prior_cost_total > 0:
        delta = cost_total - prior_cost_total
        pct = 100.0 * delta / prior_cost_total
        sign = "+" if delta >= 0 else ""
        lines.append(f"  vs. prior month: {sign}${delta:.2f} ({sign}{pct:.0f}%)")
    if cost_lines:
        lines.append("")
        lines.append("  By service:")
        for service, amount in cost_lines:
            lines.append(f"    {service:<45} ${amount:.2f}")
    else:
        lines.append("  (no cost-allocation-tagged spend found for this period)")

    lines += [
        "",
        "-" * 60,
        "Cost figures come from AWS Cost Explorer filtered to resources",
        "tagged Project=echopulpit -- untagged spend in this account (or",
        "in resources created without the tag) will not appear here.",
    ]
    return "\n".join(lines)


def lambda_handler(event, context):
    now = datetime.now(timezone.utc)
    start, end = _month_bounds(now, months_back=1)
    prior_start, prior_end = _month_bounds(now, months_back=2)

    month_label = datetime.strptime(start, "%Y-%m-%d").strftime("%B %Y")

    cost_total, cost_lines = _cost_by_service(start, end)
    prior_cost_total, _ = _cost_by_service(prior_start, prior_end)
    stats = _job_stats(start, end)

    body = _format_report(month_label, cost_total, cost_lines, prior_cost_total, stats)

    _ses.send_email(
        Source=SENDER,
        Destination={"ToAddresses": [RECIPIENT]},
        Message={
            "Subject": {"Data": f"EchoPulpit monthly report -- {month_label}"},
            "Body": {"Text": {"Data": body}},
        },
    )
    print(f"Sent monthly report for {month_label} ({stats['total']} jobs, ${cost_total:.2f})")
    return {"month": month_label, "jobs": stats["total"], "cost": cost_total}
