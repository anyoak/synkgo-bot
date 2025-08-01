# Use an official Python runtime as the base image
FROM python:3.10-slim

# Set working directory in the container
WORKDIR /app

# Copy requirements.txt to install dependencies
COPY requirements.txt .

# Install system dependencies and Python packages
RUN apt-get update && apt-get install -y \
    gcc \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy the bot script
COPY bot.py .

# Set environment variable to ensure Python output is sent straight to terminal (for logs)
ENV PYTHONUNBUFFERED=1

# Command to run the bot
CMD ["python", "main.py"]
