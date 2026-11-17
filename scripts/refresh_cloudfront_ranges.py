"""
Refresh app/utils/cloudfront_ip_ranges.json from AWS's published ranges.

Run by hand (e.g. monthly), review the diff, commit, deploy. The app never
fetches this at runtime.

    python scripts/refresh_cloudfront_ranges.py
"""
import datetime
import ipaddress
import json
import pathlib
import urllib.request

SOURCE = "https://ip-ranges.amazonaws.com/ip-ranges.json"
OUT = pathlib.Path(__file__).resolve().parents[1] / "app" / "utils" / "cloudfront_ip_ranges.json"


def main():
    with urllib.request.urlopen(SOURCE, timeout=30) as resp:
        data = json.load(resp)
    glob, regional = set(), set()
    for p in data.get("prefixes", []) + data.get("ipv6_prefixes", []):
        cidr = p.get("ip_prefix") or p.get("ipv6_prefix")
        ipaddress.ip_network(cidr)
        if p.get("service") == "CLOUDFRONT":
            glob.add(cidr)
        elif p.get("service") == "CLOUDFRONT_ORIGIN_FACING":
            regional.add(cidr)
    if not glob:
        raise SystemExit("No CLOUDFRONT ranges found; refusing to write an empty list.")
    OUT.write_text(json.dumps({
        "_source": f"{SOURCE} (services CLOUDFRONT + CLOUDFRONT_ORIGIN_FACING), createDate {data.get('createDate')}",
        "_snapshot_date": datetime.date.today().isoformat(),
        "_refresh": "Run: python scripts/refresh_cloudfront_ranges.py (writes this file). Never fetched at runtime.",
        "CLOUDFRONT_GLOBAL_IP_LIST": sorted(glob),
        "CLOUDFRONT_REGIONAL_EDGE_IP_LIST": sorted(regional),
    }, indent=1), encoding="utf-8")
    print(f"Wrote {len(glob)} global + {len(regional)} origin-facing ranges to {OUT}")


if __name__ == "__main__":
    main()
