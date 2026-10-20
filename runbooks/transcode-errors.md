---
title: Transcode exceptions and unsupported inputs
service: transcoder
symptom: transcode_stalled
updated_at: 2026-10-10
version: 1.0.0
error_code: TRANSCODE_ERROR
---
# Transcode code and input errors

An FFmpeg exit or Python exception can leave a job failed even while Redis and the worker are healthy.

## Checks

- Review the bounded exception type and FFmpeg exit code for the job.
- Confirm the uploaded media can be probed and its codec/container is supported.
- Compare the failing job's transcode options with a known successful job.
- Check the worker image and FFmpeg version when failures started after a deployment.

Do not place video bytes or unbounded logs in an LLM prompt.
