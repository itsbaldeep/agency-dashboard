FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN addgroup --system dashboard && adduser --system --ingroup dashboard dashboard

COPY --chown=dashboard:dashboard . .

USER dashboard
ENV HOME=/tmp

EXPOSE 8000

CMD ["python3", "app.py"]
