import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
INSTALLER = ROOT / "ops" / "deploy" / "install_nexus.sh"


def _retire_source() -> str:
    text = INSTALLER.read_text(encoding="utf-8")
    start = text.index("retire_legacy_units()")
    end = text.index("# end retire_legacy_units")
    return text[start:end]


def _run(tmp_path: Path, systemctl_body: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(systemctl_body, encoding="utf-8")
    systemctl.chmod(0o755)
    calls = tmp_path / "calls"
    calls.write_text("", encoding="utf-8")
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update(
        PATH=str(bin_dir) + os.pathsep + env.get("PATH", ""),
        SYSTEMD_UNIT_DIR=str(unit_dir),
        RETIRE_UNITS_UNDER_TEST="1",
        SUDO="",
        CALLS=str(calls),
    )
    script = _retire_source() + "\nretire_legacy_units\n"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, unit_dir


def test_installer_parses():
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)


def test_missing_legacy_units_are_left_alone(tmp_path):
    result, unit_dir = _run(
        tmp_path,
        """#!/bin/sh
printf '%s\\n' "$*" >> "$CALLS"
exit 1
""",
    )
    assert result.returncode == 0, result.stderr
    logged = (tmp_path / "calls").read_text(encoding="utf-8")
    assert "stop" not in logged
    assert "disable" not in logged
    assert "daemon-reload" not in logged
    assert list(unit_dir.iterdir()) == []


def test_existing_legacy_units_are_removed_and_a_second_run_is_quiet(tmp_path):
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    for name in ("nexus-task-mcp", "nexus-web"):
        (unit_dir / f"{name}.service").write_text("[Service]\nExecStart=/bin/false\n", encoding="utf-8")
    result, unit_dir = _run(
        tmp_path,
        """#!/bin/sh
printf '%s\\n' "$*" >> "$CALLS"
case "$1" in
  stop) exit 1 ;;
esac
exit 0
""",
    )
    assert result.returncode == 0, result.stderr
    assert list(unit_dir.iterdir()) == []
    logged = (tmp_path / "calls").read_text(encoding="utf-8").splitlines()
    assert logged.count("stop nexus-task-mcp.service") == 1
    assert logged.count("disable nexus-task-mcp.service") == 1
    assert logged.count("stop nexus-web.service") == 1
    assert logged.count("disable nexus-web.service") == 1
    assert logged.count("daemon-reload") == 1

    again, _ = _run(
        tmp_path,
        """#!/bin/sh
printf '%s\\n' "$*" >> "$CALLS"
exit 1
""",
    )
    assert again.returncode == 0, again.stderr
    quiet = (tmp_path / "calls").read_text(encoding="utf-8")
    assert "stop" not in quiet
    assert "daemon-reload" not in quiet
