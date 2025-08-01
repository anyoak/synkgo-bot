FROM python:3.10-slim

WORKDIR /app

# Install security updates and create appuser
RUN apt-get update && \
    apt-get upgrade -y && \
    rm -rf /var/lib/apt/lists/* && \
    useradd -m appuser

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and set ownership
COPY . .
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

CMD ["python", "main.py"]
