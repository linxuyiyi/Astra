"""Nexus-side download and native package decisions. Transfers happen on the SSH target."""

from __future__ import annotations

import base64
import json
import shlex
from pathlib import PurePosixPath
from urllib.parse import urlsplit


def package_name(arch: str, musl: bool) -> str:
    if arch not in {"x64", "arm64"}:
        raise RuntimeError("OpenCode 仅支持 Linux x64/arm64")
    return f"opencode-linux-{arch}" + ("-baseline" if arch == "x64" else "") + ("-musl" if musl else "")


def validate_url(url: str, allowed: set[str], *, scheme: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {scheme, "https"}
        or parsed.netloc.lower() not in allowed
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.query
        or any(c.isspace() for c in url)
    ):
        raise RuntimeError("制品引用了未允许的来源；请核对下载源与 NEXUS_REMOTE_NPM_ARTIFACT_HOSTS")


async def download(
    worker, url: str, target: str, *, allowed: set[str] | None = None, scheme: str | None = None
) -> None:
    parsed = urlsplit(url)
    validate_url(url, allowed if allowed is not None else {parsed.netloc}, scheme=scheme or parsed.scheme)
    await worker._ensure_safe()
    # Do not follow redirects into an unapproved origin. Configure a confirmed final URL instead.
    command = shlex.join(
        [
            "curl",
            "--disable",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--max-redirs",
            "0",
            "--connect-timeout",
            "15",
            "--max-time",
            "180",
            "--max-filesize",
            "536870912",
            "--retry",
            "2",
            "--output",
            target,
            "--url",
            url,
        ]
    )
    try:
        await worker._command(command, timeout=200)
    except RuntimeError as exc:
        raise RuntimeError(f"下载失败 [{parsed.netloc}{parsed.path}]（DNS/TLS/HTTP/重定向）：{exc}") from exc


async def read_json(worker, path: str) -> dict:
    raw = await worker.ssh.read_file(path)
    try:
        value = json.loads(raw or b"")
    except ValueError as exc:
        raise RuntimeError("远端下载的 metadata 不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("远端下载的 metadata 必须是对象")
    return value


async def extract(worker, archive: str, destination: str, *, native_binary: bool = False) -> None:
    """A fixed extraction primitive, without installation policy on the target."""
    script = (
        "import tarfile,sys,pathlib,shutil\n"
        "archive,target=sys.argv[1:]\n"
        "with tarfile.open(archive,'r:*') as packed:\n"
    )
    if native_binary:
        script += (
            " entries=[m for m in packed if m.name=='package/bin/opencode']\n"
            " if len(entries)!=1 or not entries[0].isfile() or entries[0].size>512*1024*1024:\n"
            "  raise SystemExit('平台包缺少唯一的普通 OpenCode 可执行文件')\n"
            " path=pathlib.Path(target)/'bin/opencode';path.parent.mkdir(parents=True,exist_ok=True)\n"
            " with packed.extractfile(entries[0]) as src,path.open('wb') as dst:shutil.copyfileobj(src,dst)\n"
            " path.chmod(0o755)\n"
        )
    else:
        script += " packed.extractall(target,filter='data')\n"
    await worker._command(shlex.join([worker._agent_interpreter(), "-I", "-c", script, archive, destination]))


async def install_opencode(worker, manifest: dict, prefix: str, *, arch: str, musl: bool) -> bool:
    package, version = package_name(arch, musl), manifest["opencode_version"]
    binary, marker = f"{prefix}/bin/opencode", f"{prefix}/installed.json"
    raw = await worker.ssh.read_file(marker)
    if raw:
        try:
            installed = json.loads(raw)
        except ValueError as exc:
            raise RuntimeError("已有 OpenCode 安装记录无效，已保留现场") from exc
        if installed.get("package") == package and installed.get("version") == version:
            if await worker._command(shlex.join([binary, "--version"])) != version:
                raise RuntimeError("已有受管 OpenCode 安装损坏，已保留现场")
            return False
    stage = prefix + ".stage-" + worker.operation_id
    registry = manifest["npm_registry"].rstrip("/")
    parsed = urlsplit(registry)
    allowed = {parsed.netloc.lower(), *manifest["npm_artifact_hosts"]}
    await worker._command(shlex.join(["mkdir", "-p", stage]))
    try:
        metadata = f"{stage}/metadata.json"
        await download(
            worker, f"{registry}/{package}/{version}", metadata, allowed=allowed, scheme=parsed.scheme
        )
        value = await read_json(worker, metadata)
        if value.get("name") != package or value.get("version") != version:
            raise RuntimeError("npm metadata 的包名或版本与固定目标不符")
        dist = value.get("dist", {})
        integrity = dist.get("integrity", "")
        if not isinstance(integrity, str) or not integrity.startswith("sha512-") or " " in integrity:
            raise RuntimeError("npm 制品缺少单一 SHA512 integrity")
        archive = f"{stage}/package.tgz"
        await download(worker, dist.get("tarball", ""), archive, allowed=allowed, scheme=parsed.scheme)
        digest = (await worker._command(shlex.join(["sha512sum", archive]))).split()[0]
        expected = base64.b64encode(bytes.fromhex(digest)).decode()
        if expected != integrity.removeprefix("sha512-"):
            raise RuntimeError("npm 制品 SHA512 integrity 校验失败")
        ready = f"{stage}/ready"
        await extract(worker, archive, ready, native_binary=True)
        if await worker._command(shlex.join([ready + "/bin/opencode", "--version"])) != version:
            raise RuntimeError("OpenCode 实际运行版本与固定目标不符，未替换原安装")
        await worker.ssh.write_file(
            ready + "/installed.json",
            json.dumps(
                {
                    "package": package,
                    "version": version,
                    "integrity": integrity,
                }
            ).encode(),
            mode=0o600,
        )
        backup = prefix + ".previous-" + worker.operation_id
        await worker._ensure_safe()
        await worker._command(
            f"test ! -e {shlex.quote(backup)} || {{ echo 'OpenCode 备份已存在' >&2; exit 1; }}; "
            f"if test -e {shlex.quote(prefix)}; then mv {shlex.quote(prefix)} {shlex.quote(backup)}; fi; "
            f"mv {shlex.quote(ready)} {shlex.quote(prefix)} || "
            f"{{ test ! -e {shlex.quote(backup)} || "
            f"mv {shlex.quote(backup)} {shlex.quote(prefix)}; exit 1; }}"
        )
        return True
    finally:
        await worker._command(shlex.join(["rm", "-rf", "--", stage]))


async def install_node(worker, manifest: dict, home: str, *, arch: str, libc: str) -> tuple[str, bool]:
    version, explicit = manifest["node_version"], manifest["node_binary"]
    root = (
        str(PurePosixPath(explicit).parent.parent)
        if explicit
        else (f"{home}/.local/share/nexus/workers/{worker.environment.id}/node-v{version}")
    )
    binary = explicit or f"{root}/bin/node"
    ready = await worker._command(f"{shlex.quote(binary)} --version 2>/dev/null || true")
    if ready == "v" + version:
        return root, False
    if not explicit:
        system_node = getattr(worker, "remote_facts", {}).get("tools", {}).get("node")
        if system_node:
            version_found = await worker._command(shlex.join([system_node, "--version"]))
            if version_found == "v" + version:
                return str(PurePosixPath(system_node).parent.parent), False
    if explicit:
        raise RuntimeError("显式 Node 无法运行或版本不符；不会下载替代版本")
    if libc != "glibc":
        raise RuntimeError("musl 或未知 libc 需要显式配置 NEXUS_REMOTE_NODE_BINARY，不改造系统运行库")
    folder = f"node-v{version}-linux-{arch}"
    stage = root + ".stage-" + worker.operation_id
    await worker._command(shlex.join(["mkdir", "-p", stage]))
    try:
        checksums, archive = f"{stage}/SHASUMS256.txt", f"{stage}/node.tar.xz"
        base = manifest["node_base_url"].rstrip("/") + "/v" + version
        await download(worker, base + "/SHASUMS256.txt", checksums)
        raw = await worker.ssh.read_file(checksums)
        matches = [
            line.split()[0]
            for line in (raw or b"").decode().splitlines()
            if len(line.split()) == 2 and line.split()[1] == folder + ".tar.xz"
        ]
        if len(matches) != 1:
            raise RuntimeError("Node 校验清单不含唯一的目标架构制品")
        await download(worker, base + "/" + folder + ".tar.xz", archive)
        actual = (await worker._command(shlex.join(["sha256sum", archive]))).split()[0]
        if actual != matches[0]:
            raise RuntimeError("Node SHA256 校验失败")
        await extract(worker, archive, stage + "/unpacked")
        ready = stage + "/unpacked/" + folder
        if await worker._command(shlex.join([ready + "/bin/node", "--version"])) != "v" + version:
            raise RuntimeError("Node 实际版本不符或目标平台无法运行，未替换原安装")
        backup = root + ".previous-" + worker.operation_id
        await worker._ensure_safe()
        await worker._command(
            f"test ! -e {shlex.quote(backup)} || exit 1; "
            f"if test -e {shlex.quote(root)}; then mv {shlex.quote(root)} {shlex.quote(backup)}; fi; "
            f"mv {shlex.quote(ready)} {shlex.quote(root)} || "
            f"{{ test ! -e {shlex.quote(backup)} || mv {shlex.quote(backup)} {shlex.quote(root)}; exit 1; }}"
        )
        return root, True
    finally:
        await worker._command(shlex.join(["rm", "-rf", "--", stage]))
