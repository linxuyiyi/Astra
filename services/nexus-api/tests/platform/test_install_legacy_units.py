"""平台入口仅负责 bootstrap，不生成或调用任何系统服务单元。"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def test_installer_is_a_single_python_entry():
    installer = ROOT / "ops/deploy/install_nexus.sh"
    subprocess.run(["bash", "-n", str(installer)], check=True)
    source = installer.read_text()
    assert 'exec python3 "$DEPLOY_DIR/deploy.py" "$@"' in source
    assert "systemctl" not in source and "systemd" not in source
    assert "svc_detach" not in source
