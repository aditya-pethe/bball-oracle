# The agent service. Build context is the REPO ROOT, not agent/, because the
# package is imported as `agent.*` and needs its parent on the path:
#     docker build -f agent/Dockerfile .
#
# Only agent/ is copied. The eval harness is deliberately absent: nothing in the
# service imports it (asserted by the import check in agent/tests), so PyYAML,
# psycopg2 and the whole measurement layer stay out of the production image.
#
# This container holds an Anthropic key and a service token. It does NOT hold a
# database DSN, by design -- SQL reaches Postgres only through the Next app's
# /api/internal/query, which owns the sandbox_ro connection and the safety chain
# (.agents/p4_agent.md "How the agent executes SQL"). If you find yourself adding
# SANDBOX_RO_DATABASE_URL here, something has gone wrong upstream.

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY agent/requirements.txt agent/requirements.txt
RUN pip install --no-cache-dir -r agent/requirements.txt

COPY agent/ agent/

# Non-root: the service executes no untrusted code itself, but it does hold
# credentials, and root in a container is a needless upgrade path.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /srv
USER appuser

EXPOSE 8080

# One worker. The workload is I/O-bound (model calls and one HTTP hop), so
# concurrency comes from async, not processes -- and a second worker would
# double the idle memory on a 256MB machine for no throughput.
CMD ["uvicorn", "agent.service:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
