"""Тестовая лицензия: своя пара ключей вместо ключа владельца Ариадны."""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import src.license as license_mod


def make_keypair(monkeypatch):
    """Закрытый ключ, открытый ключ которого подставлен в src.license."""
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(license_mod, 'PUBLIC_KEY', base64.b64encode(pub).decode())
    return key


def install_team_license(monkeypatch, data_dir, users=10, updates_until='2099-12-31'):
    key = make_keypair(monkeypatch)
    license_mod.LicenseStore(data_dir).install(
        license_mod.issue(key, 'Тестовая команда', users, updates_until))
    return key
