# Alternative to Railway's nixpacks build (railway.json) for anyone who
# wants to run this on their own VPS, docker-compose, Fly.io, etc.
#
# All configuration (BOT_TOKEN, DATABASE_URL, ...) comes from environment
# variables at `docker run` time - see .env.example for the full list.
# Nothing secret is baked into the image.

FROM python:3.11-slim

WORKDIR /app

# Every dependency in requirements.txt ships a prebuilt wheel for this base
# image (including psycopg2-binary), so no build-essential/libpq-dev needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Matches config/settings.py's own default so `docker run` without -e PORT
# still binds somewhere predictable.
ENV PORT=8080
EXPOSE 8080

# Migrations are a separate step, not baked into CMD, so `docker run` for a
# one-off shell/command doesn't try to migrate a database it may not even
# be pointed at. Run them explicitly before starting in production, e.g.:
#   docker run --env-file .env myimage sh -c "alembic upgrade head && python app.py"
CMD ["python", "app.py"]
