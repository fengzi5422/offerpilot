"""Fixed, verified-TLS IMAP providers; no user-controlled server endpoints."""
import imaplib
import re
import ssl
from contextlib import contextmanager
from dataclasses import dataclass

from app.domain import AppError
from app.version import VERSION


@dataclass(frozen=True)
class MailProvider:
    name: str
    host: str
    domains: tuple[str, ...]
    requires_id: bool = False


PROVIDERS = {
    'qq': MailProvider('qq', 'imap.qq.com', ('qq.com', 'foxmail.com')),
    '163': MailProvider('163', 'imap.163.com', ('163.com',), requires_id=True),
    '126': MailProvider('126', 'imap.126.com', ('126.com',), requires_id=True),
}


def provider_for_address(address, provider=None):
    if not isinstance(address, str) or not re.fullmatch(
        r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[a-z0-9.]+", address, re.I
    ):
        raise AppError('VALIDATION_ERROR', '请输入有效的 QQ、Foxmail、163 或 126 邮箱地址', 422)
    domain = address.rsplit('@', 1)[1].lower()
    selected = next((item for item in PROVIDERS.values() if domain in item.domains), None)
    if selected is None or provider is not None and provider != selected.name:
        raise AppError('VALIDATION_ERROR', '邮箱地址与所选邮箱服务不匹配，仅支持 QQ/Foxmail、163 和 126', 422)
    return selected


def validate_authorization_code(code):
    if (not isinstance(code, str) or not 1 <= len(code) <= 512 or
            any(ord(character) < 32 or ord(character) == 127 for character in code)):
        raise AppError('VALIDATION_ERROR', '请输入有效的邮箱授权码，不能包含换行或控制字符', 422)


class MailProviderAdapter:
    @contextmanager
    def open(self, address, credential):
        # Keep the historical two-argument injection contract for local tests.
        from app.accounts import QQConnection

        provider = provider_for_address(address)
        validate_authorization_code(credential)
        connection = None
        try:
            connection = imaplib.IMAP4_SSL(provider.host, 993,
                                          ssl_context=ssl.create_default_context(), timeout=30)
            try:
                connection.login(address, credential)
            except imaplib.IMAP4.abort:
                raise AppError('MAIL_NETWORK_ERROR', '邮箱连接中断，请检查网络后重试', 503) from None
            except imaplib.IMAP4.error:
                raise AppError('MAIL_AUTH_FAILED', '邮箱认证失败，请开启 IMAP 并使用邮箱授权码', 503) from None
            if provider.requires_id:
                # RFC 2971 / NetEase IMAP ID: static client metadata only, after login.
                identity = f'("name" "OfferPilot" "version" "{VERSION}" "vendor" "OfferPilot")'
                try:
                    status, _ = connection.xatom('ID', identity)
                except imaplib.IMAP4.abort:
                    raise
                except imaplib.IMAP4.error:
                    raise AppError('MAIL_CLIENT_ID_FAILED', '网易邮箱客户端识别失败，请确认已开启 IMAP 服务', 503) from None
                if status != 'OK':
                    raise AppError('MAIL_CLIENT_ID_FAILED', '网易邮箱客户端识别失败，请确认已开启 IMAP 服务', 503)
            yield QQConnection(connection)
        except (OSError, TimeoutError, imaplib.IMAP4.abort):
            raise AppError('MAIL_NETWORK_ERROR', '邮箱连接失败，请检查网络后重试', 503) from None
        except imaplib.IMAP4.error:
            raise AppError('MAIL_PROTOCOL_ERROR', '邮箱协议响应异常，请稍后重试', 503) from None
        finally:
            if connection:
                try:
                    connection.logout()
                except Exception:
                    pass
