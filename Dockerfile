FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
COPY football_tool.py .

RUN pip install --no-cache-dir -r requirements.txt

# Run the script directly. It will start the FastAPI server on $PORT and run the background scanner.
CMD ["python", "football_tool.py"]
