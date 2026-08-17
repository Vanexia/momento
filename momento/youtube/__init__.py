"""Optional YouTube authentication and resumable uploads.

A deliberate, opt-in network feature inside an otherwise local-first app.
Everything in this package is gated on the user explicitly clicking
"Connect YouTube account" in Settings — Momento makes zero outbound
network calls otherwise.

Public surface:

- ``auth``: OAuth Desktop flow + DPAPI-encrypted refresh token persistence.
  Functions: ``connect_account``, ``disconnect_account``,
  ``get_authorized_credentials``, ``fetch_channel_info``,
  ``is_connected``.
- ``uploader``: ``UploadJob`` QObject that runs a resumable
  upload on a worker thread, emitting Qt signals for progress/state/result.

Threading model: browser authentication and uploads run away from the GUI
thread. Their Qt signals queue results back to GUI-thread receivers.
"""

from __future__ import annotations
