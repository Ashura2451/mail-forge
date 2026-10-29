"""Read-only inbound acquisition adapters and a deterministic IMAP seam."""

from __future__ import annotations

import hashlib
import imaplib
import mailbox
import poplib
import re
import ssl
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mailforge.domain.inbound import InboundSourceType, SourceMessage
from mailforge.domain.provider import AuthenticationMethod
from mailforge.infrastructure.credentials import (
    CredentialError,
    CredentialResolver,
    EnvironmentCredentialResolver,
    UnsupportedAuthenticationMechanism,
    oauth_token_from_material,
)
from mailforge.infrastructure.smtp import xoauth2_initial_response


class SourceError(RuntimeError):
    pass


class CursorInvalidated(SourceError):
    pass


class ExternalSourceDisabled(SourceError):
    pass


class InboundSource(Protocol):
    source_ref: str

    def messages(
        self, cursor: str | None = None, limit: int | None = None
    ) -> Iterator[SourceMessage]: ...


class EmlFileSource:
    source_type = InboundSourceType.EML

    def __init__(
        self,
        source_ref: str,
        path: str | Path,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        stable_identity: str | None = None,
    ):
        self.source_ref = source_ref
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.stable_identity = stable_identity

    def messages(
        self, cursor: str | None = None, limit: int | None = None
    ) -> Iterator[SourceMessage]:
        if self.path.stat().st_size > self.max_bytes:
            raise SourceError("EML exceeds configured acquisition limit")
        raw = self.path.read_bytes()
        identity = self.stable_identity or (
            f"{self.path.resolve()}:{hashlib.sha256(raw).hexdigest()}"
        )
        if cursor and cursor == identity:
            return
        yield SourceMessage(self.source_ref, identity, raw, mailbox=str(self.path))


class MboxSource:
    source_type = InboundSourceType.MBOX

    def __init__(self, source_ref: str, path: str | Path, *, max_bytes: int = 10 * 1024 * 1024):
        self.source_ref = source_ref
        self.path = Path(path)
        self.max_bytes = max_bytes

    def messages(
        self, cursor: str | None = None, limit: int | None = None
    ) -> Iterator[SourceMessage]:
        box = mailbox.mbox(self.path, create=False)
        try:
            count = 0
            for key in box.iterkeys():
                if cursor is not None and str(key) <= cursor:
                    continue
                with box.get_file(key) as handle:
                    raw = handle.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    raise SourceError("MBOX message exceeds configured acquisition limit")
                yield SourceMessage(
                    self.source_ref,
                    str(key),
                    raw,
                    mailbox=str(self.path),
                    uid=str(key),
                )
                count += 1
                if limit is not None and count >= limit:
                    break
        finally:
            box.close()


@dataclass(frozen=True)
class SyntheticMailboxSource:
    source_ref: str
    items: tuple[bytes, ...]
    source_type: InboundSourceType = InboundSourceType.SYNTHETIC

    def messages(
        self, cursor: str | None = None, limit: int | None = None
    ) -> Iterator[SourceMessage]:
        start = int(cursor) + 1 if cursor is not None else 0
        stop = len(self.items) if limit is None else min(len(self.items), start + limit)
        for index in range(start, stop):
            raw = self.items[index]
            yield SourceMessage(
                self.source_ref,
                str(index),
                raw,
                mailbox="synthetic",
                uid=str(index),
            )


class FakeImapEndpoint(Protocol):
    def select(self, mailbox_name: str, readonly: bool = True) -> tuple[str, tuple[str, ...]]: ...

    def fetch(self, uid: str) -> bytes: ...

    def uids(self) -> Iterable[str]: ...


class SimulatedImapSource:
    source_type = InboundSourceType.IMAP

    def __init__(self, source_ref: str, endpoint: FakeImapEndpoint, mailbox_name: str = "INBOX"):
        self.source_ref = source_ref
        self.endpoint = endpoint
        self.mailbox_name = mailbox_name

    def messages(
        self, cursor: str | None = None, limit: int | None = None
    ) -> Iterator[SourceMessage]:
        uidvalidity, _ = self.endpoint.select(self.mailbox_name, readonly=True)
        if cursor:
            previous_uidvalidity, previous_uid = cursor.split(":", 1)
            if previous_uidvalidity != uidvalidity:
                raise CursorInvalidated("IMAP UIDVALIDITY changed")
        count = 0
        for uid in self.endpoint.uids():
            if cursor and int(uid) <= int(previous_uid):
                continue
            try:
                raw = self.endpoint.fetch(uid)
            except KeyError:
                continue
            yield SourceMessage(
                self.source_ref,
                f"{uidvalidity}:{uid}",
                raw,
                mailbox=self.mailbox_name,
                uidvalidity=uidvalidity,
                uid=uid,
                flags=tuple(getattr(self.endpoint, "flags", lambda _uid: ())(uid)),
            )
            count += 1
            if limit is not None and count >= limit:
                break


@dataclass(frozen=True)
class IMAPConnectionConfig:
    """Secret-free IMAP connection settings supplied to the runtime adapter."""

    host: str
    port: int = 993
    username: str | None = None
    password: str | None = None
    credential_ref: str | None = None
    auth_method: AuthenticationMethod = AuthenticationMethod.PASSWORD
    mailbox: str = "INBOX"
    timeout: float = 30.0
    ca_file: str | None = None
    max_message_bytes: int = 10 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("IMAP host must not be empty")
        if not 1 <= self.port <= 65_535:
            raise ValueError("IMAP port must be between 1 and 65535")
        if not self.username or not (self.password or self.credential_ref):
            raise ValueError("IMAP authenticated access requires username and credential_ref")
        if self.auth_method is AuthenticationMethod.OAUTH2 and not self.credential_ref:
            raise ValueError("IMAP OAuth2 authentication requires credential_ref")
        if not self.mailbox.strip():
            raise ValueError("IMAP mailbox must not be empty")
        if self.timeout <= 0:
            raise ValueError("IMAP timeout must be positive")
        if self.max_message_bytes <= 0:
            raise ValueError("IMAP message limit must be positive")


class ImapSource:
    """Read-only IMAP UID acquisition through Python's standard-library client."""

    source_type = InboundSourceType.IMAP

    def __init__(
        self,
        source_ref: str,
        config: IMAPConnectionConfig,
        *,
        credential_resolver: CredentialResolver | None = None,
        account_ref: str | None = None,
    ):
        self.source_ref = source_ref
        self.config = config
        self.credential_resolver = credential_resolver or EnvironmentCredentialResolver()
        self.account_ref = account_ref or source_ref
        self.mailbox_name = config.mailbox
        self.last_capabilities: tuple[str, ...] = ()

    def _context(self) -> ssl.SSLContext:
        context = ssl.create_default_context(cafile=self.config.ca_file)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def _credential(self) -> str:
        if self.config.credential_ref:
            try:
                return self.credential_resolver.resolve(self.config.credential_ref).secret
            except CredentialError as error:
                raise CredentialError("IMAP credential is unavailable") from error
        return self.config.password or ""

    def _oauth_response(self) -> bytes:
        if not self.config.credential_ref:
            raise CredentialError("OAuth credential is unavailable")
        token = oauth_token_from_material(
            self.credential_resolver.resolve(self.config.credential_ref)
        )
        if token.expired:
            raise CredentialError("OAuth access token is expired")
        return xoauth2_initial_response(self.config.username or "", token.access_token).encode()

    @staticmethod
    def _capability_names(data: object) -> tuple[str, ...]:
        values: list[str] = []
        if isinstance(data, (list, tuple)):
            for item in data:
                if isinstance(item, bytes):
                    values.extend(item.decode("ascii", "replace").split())
                elif isinstance(item, str):
                    values.extend(item.split())
        return tuple(sorted({value.upper() for value in values}))

    @staticmethod
    def _uidvalidity(client: imaplib.IMAP4) -> str:
        response = client.response("UIDVALIDITY")
        values = response[1] if isinstance(response, tuple) and len(response) > 1 else None
        if not values:
            raise SourceError("IMAP SELECT did not return UIDVALIDITY")
        value = values[0]
        return value.decode("ascii", "replace") if isinstance(value, bytes) else str(value)

    def _authenticate(self) -> imaplib.IMAP4:
        client = imaplib.IMAP4_SSL(
            self.config.host,
            self.config.port,
            ssl_context=self._context(),
            timeout=self.config.timeout,
        )
        try:
            status, capabilities = client.capability()
            if status != "OK":
                raise SourceError("IMAP CAPABILITY failed")
            self.last_capabilities = self._capability_names(capabilities)
            if self.config.auth_method is AuthenticationMethod.OAUTH2:
                authenticate = getattr(client, "authenticate", None)
                if not callable(authenticate):
                    raise UnsupportedAuthenticationMechanism("IMAP client does not support XOAUTH2")
                login_status, _ = authenticate("XOAUTH2", lambda _challenge: self._oauth_response())
            else:
                login_status, _ = client.login(self.config.username or "", self._credential())
            if login_status != "OK":
                raise SourceError("IMAP authentication failed")
            return client
        except BaseException:
            try:
                client.logout()
            except Exception:
                pass
            raise

    def _connect(
        self, *, mailbox_name: str | None = None, readonly: bool = True
    ) -> tuple[imaplib.IMAP4, str]:
        client = self._authenticate()
        selected = mailbox_name or self.config.mailbox
        select_status, _ = client.select(selected, readonly=readonly)
        if select_status != "OK":
            try:
                client.logout()
            except Exception:
                pass
            raise SourceError("IMAP mailbox selection failed")
        return client, self._uidvalidity(client)

    @staticmethod
    def _parse_list_row(item: object) -> dict[str, object] | None:
        raw = item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
        match = re.match(r"\((?P<flags>[^)]*)\)\s+\"(?P<delimiter>[^\"]*)\"\s+(?P<name>.*)$", raw)
        if not match:
            return None
        name = match.group("name").strip()
        if len(name) >= 2 and name[0] == name[-1] == '"':
            name = name[1:-1].replace('\\"', '"')
        delimiter = match.group("delimiter") or None
        parent = name.rsplit(delimiter, 1)[0] if delimiter and delimiter in name else None
        return {
            "mailbox": name,
            "parent_mailbox": parent,
            "delimiter": delimiter,
            "flags": tuple(sorted(match.group("flags").split())),
            "selectable": "\\Noselect" not in match.group("flags"),
        }

    def discover_mailboxes(self) -> list[dict[str, object]]:
        client = self._authenticate()
        try:
            status, data = client.list('""', '"*"')
            if status != "OK":
                raise SourceError("IMAP LIST failed")
            result = [parsed for item in data if (parsed := self._parse_list_row(item)) is not None]
            return result
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def mailbox_state(self, mailbox_name: str | None = None) -> dict[str, object]:
        client, uidvalidity = self._connect(mailbox_name=mailbox_name, readonly=True)
        try:
            selected = mailbox_name or self.config.mailbox
            status, data = client.status(selected, "(MESSAGES UNSEEN UIDNEXT UIDVALIDITY)")
            values: dict[str, str] = {}
            if status == "OK" and data:
                raw = (
                    data[0].decode("ascii", "replace")
                    if isinstance(data[0], bytes)
                    else str(data[0])
                )
                values = {
                    key: value
                    for key, value in re.findall(
                        r"(MESSAGES|UNSEEN|UIDNEXT|UIDVALIDITY)\s+(\d+)", raw
                    )
                }
            return {
                "mailbox": selected,
                "uidvalidity": uidvalidity,
                "uidnext": values.get("UIDNEXT"),
                "total_count": int(values["MESSAGES"]) if "MESSAGES" in values else None,
                "unseen_count": int(values["UNSEEN"]) if "UNSEEN" in values else None,
                "capabilities": self.last_capabilities,
            }
        finally:
            try:
                client.logout()
            except Exception:
                pass

    @staticmethod
    def _fetch_flags(data: object) -> tuple[str, ...]:
        values: set[str] = set()
        if isinstance(data, (list, tuple)):
            for item in data:
                raw = item.decode("ascii", "replace") if isinstance(item, bytes) else str(item)
                match = re.search(r"FLAGS \(([^)]*)\)", raw)
                if match:
                    values.update(match.group(1).split())
        return tuple(sorted(values))

    def flags_for(self, mailbox_name: str, uid: str) -> tuple[str, ...]:
        client, _ = self._connect(mailbox_name=mailbox_name, readonly=True)
        try:
            status, data = client.uid("FETCH", uid, "(FLAGS UID)")
            if status != "OK":
                raise SourceError(f"IMAP UID FLAG FETCH failed for {uid}")
            return self._fetch_flags(data)
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def _mutate(
        self, mailbox_name: str, uid: str, command: str, *arguments: str
    ) -> dict[str, object]:
        client, uidvalidity = self._connect(mailbox_name=mailbox_name, readonly=False)
        try:
            if command == "STORE":
                status, data = client.uid("STORE", uid, *arguments)
            elif command == "COPY":
                status, data = client.uid("COPY", uid, *arguments)
            elif command == "MOVE":
                if "MOVE" not in self.last_capabilities:
                    raise SourceError("IMAP UID MOVE is not advertised by this server")
                status, data = client.uid("MOVE", uid, *arguments)
            else:
                raise SourceError("unsupported IMAP mutation command")
            if status != "OK":
                raise SourceError(f"IMAP UID {command} failed")
            return {"uidvalidity": uidvalidity, "command": command, "response": str(data)}
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def mutate(
        self,
        *,
        mailbox_name: str,
        uid: str,
        action: str,
        flags: tuple[str, ...] = (),
        target_folder: str | None = None,
    ) -> dict[str, object]:
        if action == "mark_read":
            return self._mutate(mailbox_name, uid, "STORE", "+FLAGS.SILENT", r"(\Seen)")
        if action == "mark_unread":
            return self._mutate(mailbox_name, uid, "STORE", "-FLAGS.SILENT", r"(\Seen)")
        if action == "flag":
            if not flags or any(
                flag not in {r"\Seen", r"\Flagged", r"\Answered", r"\Draft"} for flag in flags
            ):
                raise SourceError("unsupported IMAP flag")
            return self._mutate(mailbox_name, uid, "STORE", "+FLAGS.SILENT", f"({' '.join(flags)})")
        if action == "unflag":
            if not flags or any(
                flag not in {r"\Seen", r"\Flagged", r"\Answered", r"\Draft"} for flag in flags
            ):
                raise SourceError("unsupported IMAP flag")
            return self._mutate(mailbox_name, uid, "STORE", "-FLAGS.SILENT", f"({' '.join(flags)})")
        if action == "copy":
            if not target_folder:
                raise SourceError("IMAP COPY requires a target folder")
            return self._mutate(mailbox_name, uid, "COPY", target_folder)
        if action in {"move", "archive"}:
            if not target_folder:
                raise SourceError("IMAP MOVE requires a target folder")
            return self._mutate(mailbox_name, uid, "MOVE", target_folder)
        raise SourceError("permanent deletion and EXPUNGE are not supported")

    def messages(
        self, cursor: str | None = None, limit: int | None = None
    ) -> Iterator[SourceMessage]:
        previous_uidvalidity: str | None = None
        previous_uid = 0
        if cursor:
            try:
                previous_uidvalidity, previous_uid_text = cursor.split(":", 1)
                previous_uid = int(previous_uid_text)
            except ValueError as error:
                raise CursorInvalidated("invalid IMAP cursor") from error
        client, uidvalidity = self._connect()
        try:
            if previous_uidvalidity is not None and previous_uidvalidity != uidvalidity:
                raise CursorInvalidated("IMAP UIDVALIDITY changed")
            status, data = client.uid("SEARCH", None, "ALL")
            if status != "OK":
                raise SourceError("IMAP UID SEARCH failed")
            if limit == 0:
                return
            raw_uids = data[0] if data else b""
            uid_values = [int(value) for value in raw_uids.split() if value.isdigit()]
            count = 0
            for uid in uid_values:
                if uid <= previous_uid:
                    continue
                status, fetched = client.uid("FETCH", str(uid), "(BODY.PEEK[] FLAGS UID)")
                if status != "OK":
                    raise SourceError(f"IMAP UID FETCH failed for {uid}")
                raw = next(
                    (
                        item[1]
                        for item in fetched
                        if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes)
                    ),
                    None,
                )
                if raw is None:
                    raise SourceError(f"IMAP UID FETCH returned no message for {uid}")
                flags = self._fetch_flags(fetched)
                if len(raw) > self.config.max_message_bytes:
                    raise SourceError(f"IMAP message {uid} exceeds configured acquisition limit")
                yield SourceMessage(
                    self.source_ref,
                    f"{uidvalidity}:{uid}",
                    raw,
                    mailbox=self.config.mailbox,
                    uidvalidity=uidvalidity,
                    uid=str(uid),
                    flags=flags,
                    account_ref=self.account_ref,
                )
                count += 1
                if limit is not None and count >= limit:
                    break
        finally:
            try:
                client.logout()
            except Exception:
                pass


class DisabledImapSource:
    source_type = InboundSourceType.IMAP

    def __init__(self, source_ref: str):
        self.source_ref = source_ref

    def messages(
        self, cursor: str | None = None, limit: int | None = None
    ) -> Iterator[SourceMessage]:
        raise ExternalSourceDisabled("real IMAP acquisition is disabled; use a qualified adapter")


@dataclass(frozen=True)
class POP3ConnectionConfig:
    """Bounded POP3S settings; the adapter never issues DELE or QUIT-after-delete."""

    host: str
    port: int = 995
    username: str = ""
    password: str | None = None
    credential_ref: str | None = None
    timeout: float = 30.0
    max_message_bytes: int = 10 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.host.strip() or not self.username:
            raise ValueError("POP3S host and username are required")
        if not 1 <= self.port <= 65_535 or self.timeout <= 0 or self.max_message_bytes <= 0:
            raise ValueError("POP3S connection limits are invalid")
        if not (self.password or self.credential_ref):
            raise ValueError("POP3S requires password or credential_ref")


class Pop3Source:
    """Read-only POP3/POP3S UIDL acquisition with bounded RETR."""

    source_type = InboundSourceType.POP3

    def __init__(
        self,
        source_ref: str,
        config: POP3ConnectionConfig,
        *,
        credential_resolver: CredentialResolver | None = None,
        use_tls: bool = True,
    ):
        self.source_ref = source_ref
        self.config = config
        self.credential_resolver = credential_resolver or EnvironmentCredentialResolver()
        self.use_tls = use_tls

    def _credential(self) -> str:
        if self.config.credential_ref:
            try:
                return self.credential_resolver.resolve(self.config.credential_ref).secret
            except CredentialError as error:
                raise CredentialError("POP3 credential is unavailable") from error
        return self.config.password or ""

    def _connect(self):
        client = (
            poplib.POP3_SSL(self.config.host, self.config.port, timeout=self.config.timeout)
            if self.use_tls
            else poplib.POP3(self.config.host, self.config.port, timeout=self.config.timeout)
        )
        try:
            client.user(self.config.username)
            client.pass_(self._credential())
            return client
        except BaseException:
            try:
                client.quit()
            except Exception:
                pass
            raise

    def messages(
        self, cursor: str | None = None, limit: int | None = None
    ) -> Iterator[SourceMessage]:
        client = self._connect()
        try:
            response, lines, _ = client.uidl()
            if not response.startswith(b"+OK"):
                raise SourceError("POP3 UIDL failed")
            items = []
            for line in lines:
                parts = line.decode("ascii", "replace").split()
                if len(parts) >= 2:
                    items.append((parts[0], parts[1]))
            if cursor and cursor not in {uidl for _, uidl in items}:
                raise CursorInvalidated("POP3 UIDL cursor is no longer present")
            started = cursor is None
            count = 0
            for number, uidl in items:
                if not started:
                    if uidl == cursor:
                        started = True
                    continue
                response, lines, _ = client.retr(int(number))
                if not response.startswith(b"+OK"):
                    raise SourceError(f"POP3 RETR failed for {uidl}")
                raw = b"\r\n".join(lines) + b"\r\n"
                if len(raw) > self.config.max_message_bytes:
                    raise SourceError(f"POP3 message {uidl} exceeds acquisition limit")
                yield SourceMessage(
                    self.source_ref,
                    uidl,
                    raw,
                    mailbox="POP3",
                    uid=uidl,
                )
                count += 1
                if limit is not None and count >= limit:
                    break
        finally:
            try:
                client.quit()
            except Exception:
                pass
