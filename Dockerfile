FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create data directory for JSON storage
RUN mkdir -p data

CMD ["python", "bot.py"]
