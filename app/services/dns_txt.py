"""
Minimal DNS TXT lookup over the standard library `socket` module.

dnspython is not installed in this project, so domain ownership checks ask a
resolver directly: UDP first, TCP when the answer is truncated. The resolver
is DNS_RESOLVER if set, else the first nameserver in /etc/resolv.conf, else
1.1.1.1. Only TXT answers for the exact name asked are returned.
"""
import os
import secrets
import socket
import struct

TIMEOUT = 5
TYPE_TXT = 16
CLASS_IN = 1


class DNSError(Exception):
    pass


class NXDomain(DNSError):
    pass


def _resolver():
    configured = os.environ.get("DNS_RESOLVER", "").strip()
    if configured:
        return configured
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    return parts[1]
    except OSError:
        pass
    return "1.1.1.1"


def _encode_name(name):
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("idna") if label else b""
        if not raw or len(raw) > 63:
            raise DNSError("invalid domain name")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def build_query(name, qid):
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)   # recursion desired
    return header + _encode_name(name) + struct.pack(">HH", TYPE_TXT, CLASS_IN)


def _skip_name(msg, offset):
    while True:
        if offset >= len(msg):
            raise DNSError("truncated name")
        length = msg[offset]
        if length & 0xC0 == 0xC0:
            return offset + 2
        if length == 0:
            return offset + 1
        offset += 1 + length


def parse_response(msg, qid):
    """TXT strings from a DNS answer. Raises NXDomain / DNSError."""
    if len(msg) < 12:
        raise DNSError("short answer")
    rid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", msg[:12])
    if rid != qid:
        raise DNSError("answer id mismatch")
    rcode = flags & 0x000F
    if rcode == 3:
        raise NXDomain("no such domain")
    if rcode != 0:
        raise DNSError(f"resolver error {rcode}")
    offset = 12
    for _ in range(qd):
        offset = _skip_name(msg, offset) + 4
    records = []
    for _ in range(an):
        offset = _skip_name(msg, offset)
        if offset + 10 > len(msg):
            raise DNSError("truncated answer")
        rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", msg[offset:offset + 10])
        offset += 10
        rdata = msg[offset:offset + rdlen]
        offset += rdlen
        if rtype != TYPE_TXT:
            continue
        parts, i = [], 0
        while i < len(rdata):
            n = rdata[i]
            parts.append(rdata[i + 1:i + 1 + n])
            i += 1 + n
        records.append(b"".join(parts).decode("utf-8", "replace"))
    return records, bool(flags & 0x0200)


def _udp(server, query):
    with socket.socket(socket.AF_INET6 if ":" in server else socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(TIMEOUT)
        s.sendto(query, (server, 53))
        data, _ = s.recvfrom(4096)
        return data


def _tcp(server, query):
    with socket.create_connection((server, 53), timeout=TIMEOUT) as s:
        s.sendall(struct.pack(">H", len(query)) + query)
        head = s.recv(2)
        if len(head) < 2:
            raise DNSError("no TCP answer")
        (length,) = struct.unpack(">H", head)
        data = b""
        while len(data) < length:
            chunk = s.recv(length - len(data))
            if not chunk:
                break
            data += chunk
        return data


def txt_records(name):
    """All TXT strings for `name`. Raises NXDomain or DNSError."""
    server = _resolver()
    qid = secrets.randbelow(65536)
    query = build_query(name, qid)
    try:
        records, truncated = parse_response(_udp(server, query), qid)
        if truncated:
            records, _ = parse_response(_tcp(server, query), qid)
    except (OSError, socket.timeout) as e:
        raise DNSError(f"lookup failed: {type(e).__name__}")
    return records
