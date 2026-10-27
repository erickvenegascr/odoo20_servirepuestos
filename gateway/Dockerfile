FROM python:3.13-slim@sha256:b3575dcf816cdacc5292df77a6e3e59b8941631078373a045efa2ac1e9c5c3b2
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY gateway/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock \
    && groupadd --gid 10001 gateway \
    && useradd --uid 10001 --gid gateway --no-create-home gateway
COPY --chown=10001:10001 gateway /app
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "30", "--access-logfile", "-"]
