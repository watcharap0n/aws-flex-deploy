# Build from repository root, only on the parent-approved cloud builder.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY _workers/customer_ingestion/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY _lambda/ingestion_data/*.py /app/
COPY _lambda/landing_data/*.py /app/landing/
COPY _workers/customer_ingestion/worker.py /app/worker.py
USER 10001
ENTRYPOINT ["python", "worker.py"]
