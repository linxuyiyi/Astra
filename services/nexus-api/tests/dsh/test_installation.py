import json

import pytest

from app.worker_agent.dsh_acp.preflight import inspect_installation


@pytest.fixture
def installation(tmp_path):
    prefix = tmp_path / "runtime"
    package = prefix / "node_modules/@deepseek-ai/dsh"
    (package / "lib").mkdir(parents=True)
    executable = package / "lib/bin.js"
    executable.touch()
    (package / "package.json").write_text(json.dumps({"name": "@deepseek-ai/dsh", "version": "0.2.1-rc.2"}))
    lock = {"packages": {"": {"name": "probe"}, "node_modules/@deepseek-ai/dsh": {"version": "0.2.1-rc.2"}}}
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps(lock))
    (prefix / "package-lock.json").write_text(json.dumps(lock))
    return prefix, executable, expected, lock


def test_exact_installed_dependency_fingerprint(installation):
    _, executable, expected, _ = installation
    result = inspect_installation(executable, expected)
    assert result["dsh"] == "0.2.1-rc.2" and result["dependency_count"] == 1


def test_modified_installed_package_not_trusted_from_lock(installation):
    _, executable, expected, _ = installation
    package = executable.parent.parent / "package.json"
    package.write_text(json.dumps({"name": "@deepseek-ai/dsh", "version": "0.2.1-rc.3"}))
    with pytest.raises(RuntimeError, match="dsh_package_version_mismatch"):
        inspect_installation(executable, expected)


def test_old_nonportable_npm_lock_is_rejected(installation):
    prefix, executable, expected, lock = installation
    lock["packages"]["../../../private/tmp/host-package"] = {"version": "1"}
    for path in (expected, prefix / "package-lock.json"):
        path.write_text(json.dumps(lock))
    with pytest.raises(RuntimeError, match="npm_lock_paths_not_portable"):
        inspect_installation(executable, expected)


def test_missing_mandatory_package_is_not_confused_with_other_platform_optional_package(installation):
    prefix, executable, expected, lock = installation
    lock["packages"]["node_modules/missing"] = {"version": "1", "optional": True}
    for path in (expected, prefix / "package-lock.json"):
        path.write_text(json.dumps(lock))
    assert inspect_installation(executable, expected)["dependency_count"] == 1
    lock["packages"]["node_modules/missing"]["optional"] = False
    for path in (expected, prefix / "package-lock.json"):
        path.write_text(json.dumps(lock))
    with pytest.raises(RuntimeError, match="locked_dependency_missing"):
        inspect_installation(executable, expected)
