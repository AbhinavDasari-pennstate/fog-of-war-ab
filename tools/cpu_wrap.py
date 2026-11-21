"""Local runs only (never the shipped CMD): run a command, pass its stdout through,
and print the CPU seconds it and its children used to stderr as `CPU_SECONDS <x>`.
"""

import resource
import subprocess
import sys


def main(cmd: list[str]) -> int:
    code = subprocess.run(cmd).returncode
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    print(f"CPU_SECONDS {r.ru_utime + r.ru_stime:.3f}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
