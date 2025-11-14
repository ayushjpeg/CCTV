FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8001

WORKDIR /app

# Install system deps for OpenCV
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1-mesa-glx libglib2.0-0 ffmpeg v4l-utils \
    && rm -rf /var/lib/apt/lists/*

# Copy app
# Copy requirements first to leverage Docker cache
COPY requirements.txt /app/requirements.txt

# Install Python deps from requirements
RUN pip install --upgrade pip setuptools wheel && \
    pip install -r /app/requirements.txt

# Copy application code
COPY . /app

EXPOSE ${PORT}

# Default env vars
ENV CCTV_PASSWORD_HASH=""
ENV CCTV_FEED_KEY=""
ENV CAMERA_INDEX=0

# Run with gunicorn on port 8001
CMD ["gunicorn", "-b", "0.0.0.0:8001", "app:app", "--workers", "2"]
