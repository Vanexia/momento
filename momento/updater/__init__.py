"""Authenticated application updates for Mementos."""

from momento.updater.metadata import (
    InstallerInfo,
    UpdateManifest,
    UpdateMetadataError,
    UpdateNotNewerError,
    UpdatePolicyError,
    UpdateSignatureError,
    parse_signed_manifest,
)

__all__ = [
    "InstallerInfo",
    "UpdateManifest",
    "UpdateMetadataError",
    "UpdateNotNewerError",
    "UpdatePolicyError",
    "UpdateSignatureError",
    "parse_signed_manifest",
]
