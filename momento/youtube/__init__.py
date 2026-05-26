"""YouTube upload bridge (Phase 11).

A deliberate, opt-in network feature inside an otherwise local-first app.
Everything in this package is gated on the user explicitly clicking
"Connect YouTube account" in Settings — Momento makes zero outbound
network calls otherwise.

Public surface:

- ``auth``: OAuth Desktop flow + DPAPI-encrypted refresh token persistence.
  Functions: ``connect_account``, ``disconnect_account``,
  ``get_authorized_credentials``, ``fetch_channel_info``,
  ``is_connected``.
- ``uploader`` (Phase 11.3): ``UploadJob`` QObject that runs a resumable
  upload on a worker thread, emitting Qt signals for progress/state/result.

Threading model: ``auth`` runs on the GUI thread (browser launch is a
user-blocking action; loopback redirect server is short-lived). Uploads
run on a worker QThread, with all Qt signal emission from that thread —
the receiver lives on the GUI thread by Qt convention.
"""

from __future__ import annotations
