import os
import logging
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("TABLE_NAME", "JobSignal")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


def get_skill_counters():
    """A single Query against the COUNTER partition -- bounded by the number
    of distinct skills, not the number of postings. No table scan required."""
    counters = []
    query_kwargs = {"KeyConditionExpression": Key("PK").eq("COUNTER")}
    while True:
        response = table.query(**query_kwargs)
        counters.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    return counters


def lambda_handler(event, context):
    counters = get_skill_counters()
    logger.info("Read %d skill counters", len(counters))

    ranked = sorted(counters, key=lambda c: int(c["article_count"]), reverse=True)
    top_skills = [
        {"skill": c["SK"], "count": int(c["article_count"])} for c in ranked[:15]
    ]
    total_tagged_postings = sum(int(c["article_count"]) for c in counters)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    table.put_item(
        Item={
            "PK": "SUMMARY",
            "SK": today,
            "date": today,
            "total_tagged_postings": total_tagged_postings,
            "top_skills": top_skills,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    result = {"date": today, "top_skills": top_skills}
    logger.info("Summary written: %s", result)
    return result
