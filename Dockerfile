FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

COPY requirements.txt .
COPY football_tool.py .

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "football_tool.py"]
