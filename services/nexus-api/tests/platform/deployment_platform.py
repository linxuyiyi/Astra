"""仅载入平台部署模块，不执行部署入口。"""

import sys
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[4] / "ops/deploy"
sys.path.insert(0, str(DEPLOY))
import configuration  # noqa: E402
import deploy  # noqa: E402
import lifecycle  # noqa: E402

__all__ = ["DEPLOY", "configuration", "deploy", "lifecycle"]
