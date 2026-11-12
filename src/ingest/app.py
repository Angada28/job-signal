import os
import json
import logging
from datetime import datetime, timezone

import boto3
import requests
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
TABLE_NAME = os.environ.get("TABLE_NAME", "JobSignal")
MAX_JOBS = int(os.environ.get("MAX_JOBS", "50"))
# TransactWriteItems allows at most 100 actions: 1 META Put + up to 99 counter Updates.
MAX_TAGS = 99

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
# Plain low-level client for TransactWriteItems. Not dynamodb.meta.client: the
# resource hooks that client to auto-serialize values, so TypeSerializer output
# ({"N": "1"}) would be serialized a second time into a map.
client = boto3.client("dynamodb")
serializer = TypeSerializer()


def normalize_tag(tag: str) -> str:
    """Lowercase and strip so 'React', 'react', ' React ' all collapse to one skill key."""
    return tag.strip().lower()


def fetch_jobs():
    resp = requests.get(
        REMOTIVE_URL,
        params={"category": "software-dev", "limit": MAX_JOBS},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("jobs", [])


def build_items(job):
    job_id = str(job["id"])
    tags = sorted({normalize_tag(t) for t in job.get("tags", []) if t.strip()})
    if len(tags) > MAX_TAGS:
        logger.warning(
            "Job %s has %d tags; keeping the first %d", job_id, len(tags), MAX_TAGS
        )
        tags = tags[:MAX_TAGS]
    now = datetime.now(timezone.utc).isoformat()
    pub_date = job.get("publication_date", now)

    canonical = {
        "PK": f"JOB#{job_id}",
        "SK": "META",
        "job_id": job_id,
        "title": job.get("title", ""),
        "company": job.get("company_name", ""),
        "url": job.get("url", ""),
        "category": job.get("category", ""),
        "job_type": job.get("job_type", ""),
        "location": job.get("candidate_required_location", ""),
        "salary": job.get("salary", ""),
        "publication_date": pub_date,
        "tags": tags,
        "source": "remotive",
        # META is only written the first time a job is seen (conditional Put),
        # so this is the "first ingested" timestamp, not the latest run.
        "ingested_at": now,
    }

    skill_items = [
        {
            "PK": f"SKILL#{tag}",
            "SK": f"{pub_date}#{job_id}",
            "job_id": job_id,
            "title": job.get("title", ""),
            "company": job.get("company_name", ""),
            "url": job.get("url", ""),
        }
        for tag in tags
    ]

    return canonical, skill_items


def record_new_job(canonical):
    """Atomically mark the job as seen AND bump its skill counters.

    One transaction: a Put of JOB#<id>/META that only succeeds if META doesn't
    exist yet, plus one ADD per tag counter. Either all of it commits or none
    of it does, so a retry after a crash can never double-count (META written
    -> condition fails) or undercount (nothing written -> retry does it all).

    Returns True if the job was new, False if it had already been ingested.
    """
    actions = [
        {
            "Put": {
                "TableName": TABLE_NAME,
                "Item": {k: serializer.serialize(v) for k, v in canonical.items()},
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        }
    ]
    for tag in canonical["tags"]:
        actions.append(
            {
                "Update": {
                    "TableName": TABLE_NAME,
                    "Key": {"PK": {"S": "COUNTER"}, "SK": {"S": tag}},
                    "UpdateExpression": "ADD article_count :inc",
                    "ExpressionAttributeValues": {":inc": {"N": "1"}},
                }
            }
        )

    try:
        client.transact_write_items(TransactItems=actions)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            reasons = e.response.get("CancellationReasons", [])
            # Index 0 is the META Put; ConditionalCheckFailed means already seen.
            if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
                return False
        raise


def lambda_handler(event, context):
    jobs = fetch_jobs()
    logger.info("Fetched %d jobs from Remotive", len(jobs))

    new_count = 0
    seen_count = 0
    failed_count = 0

    with table.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as batch:
        for job in jobs:
            job_id = str(job.get("id", "<missing id>"))
            try:
                canonical, skill_items = build_items(job)
            except Exception:
                # Failure isolation: a posting with bad data is logged and
                # skipped, and the rest of the run continues.
                failed_count += 1
                logger.exception("Skipping job %s", job_id)
                continue

            # Fan-out items are plain overwrites of the same keys, so rewriting
            # them on every run (including retries) is harmless and self-healing.
            for item in skill_items:
                batch.put_item(Item=item)

            # Any DynamoDB error other than "already seen" propagates, so Step
            # Functions retries the run; the transaction makes that retry safe.
            if record_new_job(canonical):
                new_count += 1
            else:
                seen_count += 1

    result = {
        "jobs_fetched": len(jobs),
        "new_jobs": new_count,
        "already_seen": seen_count,
        "failed": failed_count,
    }
    logger.info("Ingest complete: %s", json.dumps(result))
    return result
