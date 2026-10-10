FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gmail_to_contacts.py auth.py ./

ENV PYTHONUNBUFFERED=1

# Runs once and exits. Schedule it with cron (see README).
CMD ["python", "gmail_to_contacts.py"]
