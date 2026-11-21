"""Build-time only: download sqlglot at the pinned commit from GitHub into a directory."""

import io
import sys
import tarfile
import urllib.request

SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://github.com/tobymao/sqlglot/archive/{SHA}.tar.gz"


def main(dest: str) -> None:
    data = urllib.request.urlopen(URL, timeout=120).read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = []
        for m in tar.getmembers():
            parts = m.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            m.name = parts[1]
            members.append(m)
        tar.extractall(dest, members=members, filter="data")


if __name__ == "__main__":
    main(sys.argv[1])
