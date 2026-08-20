import os
import json
import logging
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("TABLE_NAME", "JobSignal")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)

HEADERS = {"Content-Type": "application/json"}


class DecimalEncoder(json.JSONEncoder):
    """DynamoDB returns numbers as Decimal; json.dumps doesn't know how to serialize that."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": HEADERS,
        "body": json.dumps(body, cls=DecimalEncoder),
    }


def handle_jobs(query_params):
    skill = (query_params or {}).get("skill", "").strip().lower()
    if not skill:
        return response(
            400,
            {"error": "query parameter 'skill' is required, e.g. /jobs?skill=python"},
        )

    result = table.query(
        KeyConditionExpression=Key("PK").eq(f"SKILL#{skill}"),
        ScanIndexForward=False,  # newest postings first
        Limit=25,
    )
    jobs = [
        {
            "job_id": item["job_id"],
            "title": item["title"],
            "company": item["company"],
            "url": item["url"],
        }
        for item in result.get("Items", [])
    ]
    return response(200, {"skill": skill, "count": len(jobs), "jobs": jobs})


def handle_trends(_query_params):
    result = table.query(
        KeyConditionExpression=Key("PK").eq("SUMMARY"),
        ScanIndexForward=False,  # most recent date first
        Limit=1,
    )
    items = result.get("Items", [])
    if not items:
        return response(404, {"error": "no summary has been generated yet"})

    latest = items[0]
    return response(
        200,
        {
            "date": latest["SK"],
            "total_tagged_postings": latest.get("total_tagged_postings", 0),
            "top_skills": latest.get("top_skills", []),
        },
    )


ROUTES = {
    "/jobs": handle_jobs,
    "/trends": handle_trends,
}


def lambda_handler(event, context):
    logger.info("Event: %s", json.dumps(event))
    path = event.get("rawPath", "")
    query_params = event.get("queryStringParameters") or {}

    handler = ROUTES.get(path)
    if handler is None:
        return response(404, {"error": f"no route for path '{path}'"})

    try:
        return handler(query_params)
    except Exception:
        logger.exception("Unhandled error processing request")
        return response(500, {"error": "internal server error"})
