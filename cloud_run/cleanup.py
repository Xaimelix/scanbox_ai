"""Explicit housekeeping: python -m cloud_run.cleanup --days 7 [--apply]."""
import argparse
from datetime import datetime, timedelta, timezone

from cloud_run.app import get_client
from cloud_run.control import CloudError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be >= 1")
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    client = get_client()
    token = ""
    while True:
        page = client.request("GET", f"{client.cfg.parent}/jobs", params={"pageToken": token})
        for job in page.get("jobs", []):
            if job.get("labels", {}).get("managed-by") != "scanbox-api":
                continue
            created = datetime.fromisoformat(job["createTime"].replace("Z", "+00:00"))
            if created >= cutoff:
                continue
            short_name = job["name"].rsplit("/", 1)[-1]
            if not short_name.startswith("sb-"):
                continue
            print("delete" if args.apply else "candidate", job["name"])
            if args.apply:
                try:
                    client.delete(short_name[3:])
                except CloudError as exc:
                    if exc.status not in {404, 409}:
                        raise
                    print("skip", str(exc))
        token = page.get("nextPageToken")
        if not token:
            break


if __name__ == "__main__":
    main()
