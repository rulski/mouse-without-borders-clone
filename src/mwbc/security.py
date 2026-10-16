from __future__ import annotations

import hashlib
import hmac
import ipaddress
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig, save_config
from .protocol import AuthenticationError

VALID_ROLES = {"client", "controller", "agent"}


def fingerprint(pem: str) -> str:
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def initialize_identity(config: AppConfig, config_path: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    if config.tls_certificate or config.tls_private_key:
        raise ValueError("TLS identity already configured; do not replace a paired identity implicitly")
    directory = config_path.expanduser().resolve().parent / "identity" / config.device_id
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_path = directory / "private-key.pem"
    cert_path = directory / "certificate.pem"
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, config.device_id)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    # Exclusive creation prevents silently replacing an identity after a partial setup.
    with key_path.open("xb") as handle:
        key_path.chmod(0o600)
        handle.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    with cert_path.open("xb") as handle:
        handle.write(cert.public_bytes(serialization.Encoding.PEM))
    config.tls_certificate = str(cert_path)
    config.tls_private_key = str(key_path)
    config.allow_insecure_loopback = False
    save_config(config, config_path)


def identity_bundle(config: AppConfig) -> dict[str, Any]:
    if not config.tls_certificate:
        raise ValueError("Run mwbc security init first")
    pem = Path(config.tls_certificate).read_text(encoding="ascii")
    return {"version": 1, "device_id": config.device_id, "machine_name": config.machine_name,
            "certificate": pem, "fingerprint": fingerprint(pem)}


def trust_identity(config: AppConfig, bundle: dict[str, Any], expected_fingerprint: str, roles: list[str]) -> None:
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    if not roles or not set(roles) <= VALID_ROLES:
        raise ValueError("Choose explicit client, controller, or agent roles")
    if bundle.get("version") != 1:
        raise ValueError("Unsupported public identity bundle version")
    pem = str(bundle["certificate"])
    actual = fingerprint(pem)
    if not hmac.compare_digest(actual, expected_fingerprint.lower().replace(":", "")):
        raise AuthenticationError("Fingerprint does not match the value verified on the other device")
    device_id = str(bundle["device_id"])
    name = str(bundle["machine_name"])
    if not device_id or not name or device_id == config.device_id:
        raise ValueError("Invalid remote device identity")
    cert = x509.load_pem_x509_certificate(pem.encode("ascii"))
    common_names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if len(common_names) != 1 or common_names[0].value != device_id:
        raise AuthenticationError("Certificate does not identify the exported device")
    now = datetime.now(timezone.utc)
    if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
        raise AuthenticationError("Certificate is expired or not yet valid")
    for existing in config.trusted_devices:
        if existing["device_id"] == device_id:
            if existing["fingerprint"] != actual:
                raise AuthenticationError("Certificate changed; explicitly revoke the old device first")
        elif existing["machine_name"] == name or existing["fingerprint"] == actual:
            raise AuthenticationError("Device name or certificate already belongs to another identity")
    config.trusted_devices = [item for item in config.trusted_devices if item["device_id"] != device_id]
    config.trusted_devices.append({"device_id": device_id, "machine_name": name,
                                   "certificate": pem, "fingerprint": actual, "roles": sorted(set(roles))})


def tls_context(config: AppConfig, *, server: bool, host: str) -> ssl.SSLContext | None:
    if config.allow_insecure_loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("Insecure test transport is restricted to numeric loopback addresses")
        return None
    if not config.tls_certificate or not config.tls_private_key:
        raise ValueError("TLS identity missing: run mwbc security init and approve the other device")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
    if not server:
        # Identity is an explicitly pinned device certificate, not a DHCP hostname.
        context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(config.tls_certificate, config.tls_private_key)
    if config.trusted_devices:
        context.load_verify_locations(cadata="\n".join(item["certificate"] for item in config.trusted_devices))
    return context


def verify_peer(config: AppConfig, writer: Any, *, role: str,
                device_id: str | None = None, name: str | None = None) -> dict[str, Any] | None:
    if config.allow_insecure_loopback:
        return None
    connection = writer.get_extra_info("ssl_object")
    if connection is None:
        raise AuthenticationError("Encrypted, mutually authenticated transport required")
    actual = hashlib.sha256(connection.getpeercert(binary_form=True)).hexdigest()
    for trusted in config.trusted_devices:
        if not hmac.compare_digest(actual, trusted["fingerprint"]):
            continue
        if role not in trusted["roles"]:
            raise AuthenticationError("Paired device is not approved for this role")
        if device_id is not None and device_id != trusted["device_id"]:
            raise AuthenticationError("Claimed device id does not match its certificate")
        if name is not None and name != trusted["machine_name"]:
            raise AuthenticationError("Claimed device name does not match its pairing")
        return trusted
    raise AuthenticationError("Device certificate is not explicitly paired")
