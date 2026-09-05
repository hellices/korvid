"""A throwaway CA and localhost server certificate, minted for tests.

Shared because more than one suite needs a private trust root: the
`network.ca_bundle` transport tests and the LiteLLM factory's TLS seam
test both stand up a local HTTPS server signed by a CA the system store
has never heard of.
"""

from __future__ import annotations

import datetime
import ipaddress
from pathlib import Path


def mint_ca_and_server_cert(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A test CA plus a localhost server cert signed by it."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    def _key() -> rsa.RSAPrivateKey:
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    now = datetime.datetime.now(datetime.UTC)
    ca_key = _key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "korvid test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        # 3.13's strict verification requires SKI/AKI and key-usage bits.
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    srv_key = _key()
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            # The IP SAN is what lets the tests connect to 127.0.0.1
            # directly. Resolving `localhost` is not deterministic: on
            # Windows it answers ::1 first, the test server binds IPv4 only,
            # and the connection hangs until the timeout instead of failing
            # the way the test expects.
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(srv_key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = tmp_path / "ca.pem"
    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_pem = tmp_path / "server.pem"
    cert_pem.write_bytes(srv_cert.public_bytes(serialization.Encoding.PEM))
    key_pem = tmp_path / "server-key.pem"
    key_pem.write_bytes(
        srv_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return ca_pem, cert_pem, key_pem
