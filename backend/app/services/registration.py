from __future__ import annotations

import asyncio
import hmac
import secrets
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.errors import ApiError
from app.repositories.models import RegistrationChallenge, User
from app.services.auth import as_utc, utcnow

CODE_TTL_SECONDS = 600
RESEND_SECONDS = 60
MAX_ATTEMPTS = 5
MAX_SENDS_PER_HOUR = 5


class RegistrationMailer:
    """Use the administrator's active encrypted email configuration."""

    async def send(self, runtime, *, email: str, code: str) -> None:
        try:
            config = await runtime.config_cache.get("email")
        except Exception as exc:
            raise ApiError(
                503, "EMAIL_NOT_CONFIGURED", "验证邮件服务暂不可用，请联系管理员"
            ) from exc
        if not config.values.get("enabled"):
            raise ApiError(503, "EMAIL_NOT_CONFIGURED", "验证邮件服务尚未启用，请联系管理员")
        subject = "Sub2Image 注册邮箱验证码"
        body = (
            f"你的 Sub2Image 注册验证码是：{code}\n\n"
            "验证码 10 分钟内有效，仅用于验证此邮箱。请勿将验证码告知他人。\n"
            "如果不是你本人操作，请忽略这封邮件。"
        )
        try:
            if config.values["provider"] == "api":
                async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                    response = await client.post(
                        config.values["api_base_url"],
                        headers={"Authorization": f"Bearer {config.secrets.get('api_key', '')}"},
                        json={
                            "from": config.values["from_email"],
                            "to": [email],
                            "subject": subject,
                            "text": body,
                        },
                    )
                    response.raise_for_status()
            else:
                await asyncio.to_thread(self._smtp_send, config, email, subject, body)
        except Exception as exc:
            # Provider errors may include credentials or the message body; never return them.
            raise ApiError(
                503, "EMAIL_DELIVERY_FAILED", "验证码发送失败，请稍后重试或联系管理员检查邮件配置"
            ) from exc

    @staticmethod
    def _smtp_send(config, email: str, subject: str, body: str) -> None:
        values = config.values
        message = EmailMessage()
        message["From"] = values["from_email"]
        message["To"] = email
        message["Subject"] = subject
        message.set_content(body)
        port = int(values["port"])
        tls_context = ssl.create_default_context()
        connection = (
            smtplib.SMTP_SSL(values["host"], port, timeout=15, context=tls_context)
            if port == 465
            else smtplib.SMTP(values["host"], port, timeout=15)
        )
        with connection as client:
            client.ehlo()
            if port != 465 and values["use_tls"]:
                client.starttls(context=tls_context)
                client.ehlo()
            if values.get("username"):
                client.login(values["username"], config.secrets.get("password", ""))
            refused = client.send_message(message)
            if refused:
                raise RuntimeError("recipient refused")


class RegistrationService:
    async def send_code(self, session, *, service, mailer, runtime, email: str) -> None:
        if await session.scalar(select(User.id).where(User.email == email)):
            raise ApiError(409, "USER_ALREADY_EXISTS", "邮箱已被使用，请登录或联系管理员")
        challenge = await session.get(RegistrationChallenge, email, with_for_update=True)
        now = utcnow()
        if challenge:
            retry = RESEND_SECONDS - int((now - as_utc(challenge.last_sent_at)).total_seconds())
            if retry > 0:
                raise ApiError(
                    429,
                    "CODE_SEND_TOO_SOON",
                    f"请在 {retry} 秒后重新获取验证码",
                    {"retry_after": retry},
                )
            if as_utc(challenge.window_started_at) + timedelta(hours=1) > now:
                if challenge.send_count >= MAX_SENDS_PER_HOUR:
                    retry = max(
                        1,
                        int(
                            (
                                as_utc(challenge.window_started_at) + timedelta(hours=1) - now
                            ).total_seconds()
                        ),
                    )
                    raise ApiError(
                        429,
                        "CODE_SEND_LIMIT",
                        "该邮箱获取验证码过于频繁，请稍后再试",
                        {"retry_after": retry},
                    )
            else:
                challenge.window_started_at = now
                challenge.send_count = 0
        else:
            challenge = RegistrationChallenge(email=email, window_started_at=now, send_count=0)
            session.add(challenge)
        code = f"{secrets.randbelow(1_000_000):06d}"
        challenge.nonce = secrets.token_hex(16)
        challenge.code_hash = service.token_hash(f"signup:{email}:{challenge.nonce}:{code}")
        challenge.last_sent_at = now
        challenge.expires_at = now + timedelta(seconds=CODE_TTL_SECONDS)
        challenge.send_count += 1
        challenge.attempts = 0
        challenge.consumed_at = None
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise ApiError(
                429,
                "CODE_SEND_TOO_SOON",
                "验证码正在发送，请稍后再试",
                {"retry_after": RESEND_SECONDS},
            ) from exc
        await mailer.send(runtime, email=email, code=code)
        await session.commit()

    async def verify(self, session, *, service, email: str, code: str) -> None:
        challenge = await session.get(RegistrationChallenge, email, with_for_update=True)
        now = utcnow()
        if (
            not challenge
            or challenge.consumed_at is not None
            or as_utc(challenge.expires_at) <= now
        ):
            raise ApiError(400, "EMAIL_CODE_EXPIRED", "验证码无效或已过期，请重新获取")
        if challenge.attempts >= MAX_ATTEMPTS:
            raise ApiError(400, "EMAIL_CODE_LOCKED", "验证码错误次数过多，请重新获取")
        expected = service.token_hash(f"signup:{email}:{challenge.nonce}:{code}")
        if not hmac.compare_digest(expected, challenge.code_hash):
            challenge.attempts += 1
            await session.commit()
            raise ApiError(400, "EMAIL_CODE_INVALID", "邮箱验证码错误，请检查后重试")
        # Consumption, user creation, membership and points commit atomically.
        challenge.consumed_at = now
