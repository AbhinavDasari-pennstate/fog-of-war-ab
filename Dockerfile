# Blast Radius entry (Fog of War). Built on linux/amd64 by the scorer.
# Base: the organisers' digest, so Python and SQLite match the labelled environment.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot at the pinned commit (GitHub archive of that exact SHA).
COPY tools/fetch_sqlglot.py /tmp/fetch_sqlglot.py
RUN python /tmp/fetch_sqlglot.py /app/sqlglot-src && rm /tmp/fetch_sqlglot.py

COPY selector /app/selector
COPY maps /app/maps
RUN python -m compileall -q /app/selector /app/maps /app/sqlglot-src && chmod -R a+rX /app
# The base image ships the standard library without .pyc files and the run has a read-only disk, so every run would
# compile ast, json, threading and more from source again (about 50 ms of CPU, charged to the score).
RUN python -m compileall -q -x 'site-packages|idlelib|tkinter|turtledemo|ensurepip|lib2to3' /usr/local/lib/python3.13 || true

ENV PYTHONDONTWRITEBYTECODE=1 BR_PROBE=D
CMD ["python", "-S", "-E", "/app/selector/main.py"]
