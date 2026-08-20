import os
import logging
from collections import Counter
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("TABLE_NAME", "JobSignal")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


def scan_skill_items():
    """Scan for every SKILL# item, paginating past DynamoDB's 1MB-per-page limit."""
    items = []
    scan_kwargs = {"FilterExpression": Attr("PK").begins_with("SKILL#")}
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    return items


def lambda_handler(event, context):
    items = scan_skill_items()
    logger.info("Scanned %d skill-tagged postings", len(items))

    skill_counts = Counter(item["PK"].removeprefix("SKILL#") for item in items)
    top_skills = skill_counts.most_common(15)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    table.put_item(
        Item={
            "PK": f"SUMMARY",
            "SK": today,
            "date": today,
            "total_tagged_postings": len(items),
            "top_skills": [{"skill": s, "count": c} for s, c in top_skills],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    result = {"date": today, "top_skills": top_skills}
    logger.info("Summary written: %s", result)
    return result
