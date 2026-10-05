"""data/ · reports/ 변경분을 커밋하고 푸시 (Linux/macOS/Windows 공통)."""
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], check=check, text=True, capture_output=True)


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "업데이트"
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git("add", "data", "reports")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        print("변경 없음")
        return 0
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
    git("commit", "-m", f"data: {today} {label}")
    push = git("push", check=False)
    print(push.stdout, push.stderr)
    return push.returncode


if __name__ == "__main__":
    raise SystemExit(main())
