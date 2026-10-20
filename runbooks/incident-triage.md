---
title: Transcode incident triage
service: transcoder
symptom: transcode_stalled
updated_at: 2026-10-10
version: 1.0.0
error_code: INSUFFICIENT_EVIDENCE
---
# Transcode incident triage

Start with the exact job, worker, and time window. Separate reported facts from assumptions.

## Checks

- Verify the job state and the last observed worker activity.
- Check broker connectivity, worker termination events, and bounded task error details.
- Compare timestamps across application events and infrastructure observations.
- If sources disagree or are unavailable, record the evidence gap and request human review.

Recommendations from this document are operator checks; no repair action is automated.
