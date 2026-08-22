"""
WSGI entry point — for gunicorn / uWSGI production servers.

Usage (local):
  gunicorn wsgi:app --workers 4 --bind 0.0.0.0:5000 --worker-class sync

Usage (Vercel):
  Vercel uses index.py automatically via vercel.json.
"""
from app import create_app

app = create_app()
