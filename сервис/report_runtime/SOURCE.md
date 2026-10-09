# Provenance

Vendored from Коммерческий директор/cloud_control on 2026-10-06, stdlib only.
No production data, credentials, databases or sessions are included.

- outbox.py SHA256: 9f4f521d54751a750132daa5a73b47f8adee1c285c572f0b7af2bf13aacac0a4
- telegram_transport.py SHA256: ca1d6105d22fa263fd71da5716bbd4c8f8aa00ac278aa3a161d37f5d541c70ab

The owning project runs tests/test_cloud_outbox.py and tests/test_cloud_transports.py.
Update by reviewing both source hashes and re-running those tests. Never copy
runtime SQLite files or auth directories into this package.
