Public CA certificate for MAX API only
======================================

Source (HTTPS verified, downloaded 2026-10-12):
https://gu-st.ru/content/Other/doc/russian_trusted_root_ca.cer

PEM file SHA-256 (line endings normalized to LF):
0819977502d9aed2234830f6ffb91f82f401d3674c6e51dd19e16d8b3dbf0eb4

MAX documentation requires this CA for https://platform-api2.max.ru:
https://dev.max.ru/docs-api

It is loaded into the MAX client's SSL context, not into the operating
system's global trust store. Certificate verification and hostname checks
remain enabled. Do not use curl -k or disable TLS verification to deploy.

Certificate DER SHA-256: d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31
