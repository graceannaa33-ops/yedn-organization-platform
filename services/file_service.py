"""Secure file uploads and private file delivery.

- Only PDF / JPG / JPEG / PNG are accepted.
- The file's first bytes ("magic number") must match its extension, so a
  renamed .exe or .html is rejected even if it is called photo.jpg.
- Files get random names and are stored in UPLOAD_FOLDER, which is NOT the
  static folder, so they can never be reached by a public URL.
- Files are only ever sent back as downloads/images with headers that stop
  browsers from executing or indexing them.

Storage backends (config FILE_STORAGE):
- "local"    files are written to UPLOAD_FOLDER (default; best for your own PC).
- "database" file contents go into the stored_files table. Use this on hosts
             whose disk is wiped on restart (Render free plan). Files saved
             earlier on disk are still found, so switching is safe.
"""
import hashlib
import os
import uuid

from flask import Response, abort, current_app, send_file
from werkzeug.security import safe_join

ALLOWED_TYPES = {
    "pdf": ("application/pdf", (b"%PDF-",)),
    "png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    "jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
}
IMAGE_TYPES = ("png", "jpg", "jpeg")
DOC_TYPES = ("pdf", "png", "jpg", "jpeg")


class UploadError(Exception):
    pass


def folder_path(folder):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], folder)
    os.makedirs(path, exist_ok=True)
    return path


def has_file(file_storage):
    return file_storage is not None and bool(getattr(file_storage, "filename", ""))


def save_upload(file_storage, folder, allowed=DOC_TYPES):
    if not has_file(file_storage):
        raise UploadError("Please choose a file to upload.")
    original = os.path.basename(file_storage.filename)[:255]
    ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
    if ext not in allowed or ext not in ALLOWED_TYPES:
        raise UploadError("File type not allowed. Use: " + ", ".join(a.upper() for a in allowed) + ".")
    max_size = current_app.config["MAX_FILE_SIZE"]
    data = file_storage.stream.read(max_size + 1)
    if len(data) == 0:
        raise UploadError("The file is empty.")
    if len(data) > max_size:
        raise UploadError(f"The file is too large. Maximum size is {max_size // (1024 * 1024)} MB.")
    mime, signatures = ALLOWED_TYPES[ext]
    if not any(data.startswith(sig) for sig in signatures):
        raise UploadError("The file content does not match its type. Upload a real PDF, JPG or PNG file.")
    stored_name = f"{uuid.uuid4().hex}.{ext}"
    _store(folder, stored_name, mime, data)
    return {
        "stored_name": stored_name,
        "original_name": original,
        "mime_type": mime,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def save_bytes(data, folder, ext):
    """Used by the demo seed to create placeholder files."""
    mime, _ = ALLOWED_TYPES[ext]
    stored_name = f"{uuid.uuid4().hex}.{ext}"
    _store(folder, stored_name, mime, data)
    return {"stored_name": stored_name, "mime_type": mime, "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def storage_backend():
    return "database" if current_app.config.get("FILE_STORAGE") == "database" else "local"


def _store(folder, stored_name, mime, data):
    if storage_backend() == "database":
        from extensions import db
        from models import StoredFile
        # Saved together with the record that refers to it (same database transaction).
        db.session.add(StoredFile(folder=folder, stored_name=stored_name, mime_type=mime,
                                  size_bytes=len(data), data=data))
        return
    path = os.path.join(folder_path(folder), stored_name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def send_private(folder, stored_name, mime_type, download_name=None, inline=False):
    response = None
    if storage_backend() == "database":
        from models import StoredFile
        row = StoredFile.query.filter_by(folder=folder, stored_name=stored_name).first()
        if row is not None:
            from werkzeug.utils import secure_filename
            name = secure_filename(download_name or stored_name) or stored_name
            response = Response(row.data, mimetype=mime_type)
            response.headers["Content-Disposition"] = f'{"inline" if inline else "attachment"}; filename="{name}"'

    if response is None:  # local storage, or a file saved to disk before switching to the database
        path = safe_join(folder_path(folder), stored_name)
        if path is None or not os.path.isfile(path):
            abort(404)
        response = send_file(path, mimetype=mime_type, as_attachment=not inline,
                             download_name=download_name or stored_name, max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'"
    return response
