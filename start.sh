#!/bin/bash
set -e

cd config
echo "==> Running migrations"
python manage.py migrate --no-input --verbosity 2

# One-time data import: set LOAD_FIXTURE=1 in Railway env vars for the
# first deploy, then REMOVE it so data isn't re-imported on every restart.
if [ "$LOAD_FIXTURE" = "1" ]; then
  echo "==> Loading initial data from data_dump.json"
  python manage.py loaddata ../data_dump.json --verbosity 2
  echo "==> Data loaded! REMOVE the LOAD_FIXTURE env var now."
fi

echo "==> Creating superuser (if env vars set)"
python manage.py create_superuser_from_env
echo "==> Collecting static files"
python manage.py collectstatic --no-input
echo "==> Starting gunicorn"
exec gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 4 --timeout 120
