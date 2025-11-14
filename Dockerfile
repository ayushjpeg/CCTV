FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8001

WORKDIR /app

# Install system deps for OpenCV
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 libglib2.0-0 ffmpeg v4l-utils \
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

# Run Flask with SocketIO (threading mode)
# For production, use gunicorn with python-socketio worker or uWSGI with gevent
CMD ["python", "-u", "app.py"]
