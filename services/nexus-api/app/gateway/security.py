"""API 中转凭据安全：主密钥加密与 Gateway Key 摘要。

本模块刻意不导入 settings，避免配置校验时形成循环依赖；所有函数都接收
原始字符串，由调用方（settings / 依赖注入）决定取值来源。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

GATEWAY_KEY_ALGO_PREFIX = "agw_"
GATEWAY_KEY_RANDOM_BYTES = 32
GATEWAY_KEY_PREFIX_LENGTH = 12

class CredentialError(Exception):
    """节点凭据主密钥不可用。"""


class CredentialUnavailable(CredentialError):
    """未配置主密钥，禁止涉及节点凭据的写操作。"""


def generate_gateway_key() -> str:
    return GATEWAY_KEY_ALGO_PREFIX + secrets.token_urlsafe(GATEWAY_KEY_RANDOM_BYTES)


def gateway_key_prefix(raw_key: str) -> str:
    return raw_key[:GATEWAY_KEY_PREFIX_LENGTH]


def gateway_key_digest(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def verify_gateway_key(raw_key: str, digest: str) -> bool:
    return hmac.compare_digest(gateway_key_digest(raw_key), digest)


class CredentialCipher:
    """使用 Fernet 主密钥加密节点下游凭据，库中只保存密文。"""

    def __init__(self, master_key: str | None) -> None:
        normalized = (master_key or "").strip()
        self._fernet: Fernet | None = None
        if normalized:
            try:
                self._fernet = Fernet(normalized.encode("ascii"))
            except Exception as exc:  # noqa: BLE001 - Fernet 对非法 key 抛 ValueError
                raise CredentialError("ASTRA_GATEWAY_MASTER_KEY 不是合法的 Fernet 主密钥") from exc

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def require(self) -> None:
        if self._fernet is None:
            raise CredentialUnavailable("未配置 ASTRA_GATEWAY_MASTER_KEY，无法加密或解密节点凭据")

    def encrypt(self, plaintext: str) -> str:
        self.require()
        assert self._fernet is not None
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        self.require()
        assert self._fernet is not None
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise CredentialError("节点凭据无法用当前主密钥解密") from exc