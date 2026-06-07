FROM python:3.11-slim

# Install FFmpeg + dependencies
RUN apt-get update && \
    apt-get install -y ffmpeg wget && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements dulu
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy semua file
COPY . .

# Bikin folder temp
RUN mkdir -p /app/downloads /app/output

EXPOSE 5000

# Run
CMD ["python", "worker.py"]
