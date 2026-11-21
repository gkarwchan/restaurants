FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ahs_scraper.py cloud_run.py geocode.py ./

RUN useradd --system --create-home app && mkdir /data && chown app /data
USER app

ENTRYPOINT ["python", "cloud_run.py"]
