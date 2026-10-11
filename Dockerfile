FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 10001 collector \
    && mkdir /data && chown collector /data

WORKDIR /app
COPY billcheck_collector/ /app/billcheck_collector/

USER collector
VOLUME /data
ENTRYPOINT ["python", "-m", "billcheck_collector"]
