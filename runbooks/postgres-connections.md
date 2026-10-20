---
title: PostgreSQL connection exhaustion
service: transcoder
symptom: database_unavailable
error_code: POSTGRES_CONNECTION_EXHAUSTION
updated_at: 2026-10-20
version: 1.2.0
---
# PostgreSQL connection exhaustion

## Symptoms

Applications report too many clients already, remaining connection slots are reserved, pool timeout or failed database connections. A connection-refused error may instead mean PostgreSQL is down.

## Investigation checklist

- Correlate sanitized application connection errors and timestamps with PostgreSQL readiness.
- An authorized DBA inspects pg_stat_activity and max_connections on the affected video-platform or AIOps database.
- Compare SQLAlchemy pool sizes, number of replicas and connection age; inspect long or idle-in-transaction sessions.
- Separate owner, runtime and source-reader roles; never copy connection URLs or credentials into RAG.

## Distinguishing evidence

The explicit too many clients message plus a connection count near max_connections supports exhaustion. DNS/connection refused without high connection counts points to network or service availability. Missing DBA access is an evidence gap, not proof of healthy PostgreSQL.

## Likely causes

Leaked application connections, excessive per-replica pools, stalled transactions or an increase in clients exhausted available slots.

## Safe manual remediation

- Operator reviews and reduces reviewed client pressure and fixes connection lifecycle or pool limits.
- A DBA reviews any targeted session termination; never kill arbitrary sessions, drop databases or increase global limits automatically.
- Preserve incident/evidence tables and PVCs. Do not connect to another project's database to test a hypothesis.

## Verification

Require new runtime connections and an authorized original-video transaction to succeed; monitor connection headroom and latency for recurrence. Confirm original jobs and Incident evidence remain accessible.

## Escalation

Escalate to a DBA if privileged capacity checks are unavailable, transactions may be lost, or recovery needs session termination or configuration changes.

## Current policy scope

There is no executable PostgreSQL repair action in the current allowlist. These recommendations require separate operator review. Existing worker HPA may manage replicas; never invent a repair action from this document.
