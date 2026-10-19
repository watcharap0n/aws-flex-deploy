FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir boto3==1.40.43 psycopg2-binary==2.9.10 \
    email-validator==2.3.0 phonenumbers==9.0.14 pycountry==24.6.1 \
    python-dateutil==2.9.0.post0
WORKDIR /app
COPY scripts/staging_raw_clickhouse_mirror.py /app/mirror.py
COPY docker/staging-raw-clickhouse/identity_stitcher /app/identity_stitcher
USER 10001
CMD ["python", "/app/mirror.py"]
