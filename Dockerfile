FROM python:3.10.12-bullseye

# system deps for common Python packages (optional but safe)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    libssl-dev \
    libjpeg-dev \
    zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# copy app and requirements
COPY . /app

# use pip wheel cache for faster builds
ENV PIP_NO_CACHE_DIR=1
RUN pip install --upgrade pip setuptools wheel
RUN pip install -r requirements.txt

EXPOSE 5000

# use gunicorn (Procfile can override)
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "app:app"]
