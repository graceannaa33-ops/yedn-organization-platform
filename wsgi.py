"""Production entry point.

Render / any Linux host:   gunicorn wsgi:app
Local Windows development: python app.py   (Gunicorn does not run on Windows)
"""
from app import app

if __name__ == "__main__":
    app.run()
