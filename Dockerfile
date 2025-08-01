# Use official Python runtime as base image
FROM python:3.11-slim-bullseye

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV TZ=UTC
ENV DB_PATH=/data

# Create data directory for persistent storage
RUN mkdir -p ${DB_PATH}
VOLUME ${DB_PATH}

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gcc \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements file first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Set database file path in container
ENV DB_FILE=${DB_PATH}/synkgo_db.json

# Run the bot
CMD ["python", "main.py"]
