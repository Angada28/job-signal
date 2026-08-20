import os
import json
import logging
from datetime import datetime, timezone

import boto3
import requests

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
TABLE_NAME = os.environ.get("TABLE_NAME", "JobSignal")
MAX_JOBS = int(os.environ.get("MAX_JOBS", "50"))

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


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


def increment_skill_counters(tags):
    """Atomically bump each skill's running total. Called only for genuinely
    new jobs, since a job's tags are already reflected in the counters once
    it's been ingested — incrementing again on a later run would double-count."""
    for tag in tags:
        table.update_item(
            Key={"PK": "COUNTER", "SK": tag},
            UpdateExpression="ADD article_count :inc",
            ExpressionAttributeValues={":inc": 1},
        )


def lambda_handler(event, context):
    jobs = fetch_jobs()
    logger.info("Fetched %d jobs from Remotive", len(jobs))

    new_count = 0
    seen_count = 0

    with table.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as batch:
        for job in jobs:
            job_id = str(job["id"])

            existing = table.get_item(Key={"PK": f"JOB#{job_id}", "SK": "META"}).get(
                "Item"
            )
            is_new = existing is None
            if is_new:
                new_count += 1
            else:
                seen_count += 1

            canonical, skill_items = build_items(job)
            batch.put_item(Item=canonical)
            for item in skill_items:
                batch.put_item(Item=item)

            if is_new:
                increment_skill_counters(canonical["tags"])

    result = {
        "jobs_fetched": len(jobs),
        "new_jobs": new_count,
        "already_seen": seen_count,
    }
    logger.info("Ingest complete: %s", json.dumps(result))
    return result
