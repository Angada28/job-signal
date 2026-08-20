# What I learned building Job Signal

A short reference for talking about this project in interviews — the real
decisions and problems, not a feature list.

## Why I built it

I had a year of full-stack experience (Python, Go, React, healthcare data
integrations) but no hands-on AWS or workflow-orchestration experience, which
kept showing up as a gap against roles I wanted. Rather than isolated tutorials,
I built one real pipeline that touches Lambda, API Gateway, Step Functions,
DynamoDB, EventBridge, and IAM together, the way they'd actually be used.

## Architecture decisions I can defend

**Single-table DynamoDB design.** Coming from relational databases, my first
instinct was to model entities first and query later. DynamoDB forces the
opposite: design the table around your access patterns before writing any code.
I ended up with one table holding three item "shapes" distinguished by key
prefix — canonical job records, per-skill index records (fan-out on write, so
"jobs tagged python" is a direct `Query`, not a scan), and a time-series summary
item. I can explain the trade-off: more complexity on write, much simpler and
faster reads.

**Step Functions over chaining Lambdas directly.** The pipeline is only two
steps (ingest, summarize), which I could have wired with one Lambda invoking the
next. I used Step Functions instead specifically to get built-in retry/backoff
and a `Catch` branch — if ingest fails, summarize doesn't run against stale
data, and the failure is visible in the execution history instead of silently
propagating. Explicit state machine over implicit function chaining.

**Least-privilege IAM per function.** Each Lambda has its own execution role:
ingest and summarize get read+write on the DynamoDB table, query gets read-only,
and the state machine's role can only invoke those two specific functions —
nothing broader. I set this up with SAM's policy templates
(`DynamoDBCrudPolicy`, `DynamoDBReadPolicy`, `LambdaInvokePolicy`) rather than
hand-writing IAM JSON.

## Problems I actually hit (and what they taught me)

- **`sam build` failed locally because my WSL Python version (3.10) didn't
  match the Lambda runtime (3.13).** Fixed by using `sam build --use-container`,
  which builds inside a container matching the real Lambda environment instead
  of relying on the local interpreter — a better habit anyway, since it
  guarantees what builds locally will actually run in the cloud.

- **`sam local invoke` failed with `ResourceNotFoundException` even though the
  table existed.** The Lambda's `TABLE_NAME` environment variable is set via a
  CloudFormation `!Ref`, which only resolves through an actual deployed stack —
  local invoke doesn't have access to that resolution and needs the value
  supplied explicitly via `--env-vars`. Small thing, but a good example of
  where "local" testing still has edges that don't perfectly mirror deployed
  behavior.

- **A DynamoDB key design mistake surfaced only once I wrote the read side.**
  I originally wrote daily summaries as `PK: SUMMARY#<date>`, which seemed fine
  until I needed to query for "the most recent one" and realized `Query`
  requires an exact partition key match — with a new partition every day, there
  was no single query that could find the latest. Fixing it to `PK: SUMMARY`,
  `SK: <date>` (one fixed partition, sortable by date) made "give me the latest"
  a single `Query` with `Limit=1`. This is the kind of mistake that's invisible
  until you actually build the second access pattern — a real argument for
  designing around access patterns up front, not after the fact.

## An optimization that looked broken but wasn't

After the initial build worked end to end, I replaced the summarize Lambda's
full-table `Scan` (recomputing skill counts from every tagged posting, every
run) with atomic per-skill counters updated incrementally by the ingest Lambda
via `UpdateItem` with an `ADD` expression — `Query`-able in one call, bounded by
the number of distinct skills rather than the number of postings.

The counters had to increment only for genuinely _new_ jobs, not ones already
seen on a prior run, or a job would get double-counted every time the pipeline
re-ingested it. After wiring that up and deploying, I ran the pipeline and
`/trends` came back completely empty — zero counters, zero total.

My first instinct was that something was broken. Instead of guessing, I pulled
the actual Step Functions execution history and checked what the ingest step
had reported: `"new_jobs": 0, "already_seen": 17`. Remotive had simply
returned the same postings as the previous run, so correctly, nothing new was
counted. The empty result wasn't a bug — it was proof the dedup guard was
working exactly as designed. It would have been easy to assume failure and
start changing code that didn't need changing; checking the actual execution
data first instead of my assumption was the right call, and it's the same
instinct I'd want to bring to debugging a production system.

- The summarize Lambda re-scans the whole table on every run. Fine at this
  scale; at real scale I'd maintain a running counter per skill, updated
  incrementally by the ingest Lambda instead of recomputed from scratch.
- Only one data source right now (Remotive). A second source with a differently
  shaped API would be a good test of whether the normalization layer actually
  generalizes or was accidentally tailored to Remotive's response format.
- No automated tests yet — the verification so far has been real deploys and
  real HTTP/CLI calls against live infrastructure, which is a legitimate way to
  validate a small serverless project but wouldn't scale to a team codebase.
