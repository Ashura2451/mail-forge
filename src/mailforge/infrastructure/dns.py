"""Controlled DNS/MX enrichment with sync, async, cache, and offline modes."""

from __future__ import annotations

import asyncio
import json
import socket
import time
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path
from threading import Lock

from mailforge.domain.sorting import DNSMode, DNSResponse, DNSResult, MXRecord
from mailforge.infrastructure.providers import ProviderCatalog

try:
    import aiodns
except ImportError:  # pragma: no cover - optional dependency
    aiodns = None
try:
    import dns.exception
    import dns.resolver
except ImportError:  # pragma: no cover - optional dependency
    dns = None


class DNSCache:
    def __init__(self, max_size: int = 100_000):
        self.max_size = max_size
        self._values: OrderedDict[str, DNSResponse] = OrderedDict()
        self._lock = Lock()
        self.hits = 0
        self.misses = 0

    def get(self, domain: str) -> DNSResponse | None:
        with self._lock:
            value = self._values.get(domain)
            if value is None:
                self.misses += 1
                return None
            self._values.move_to_end(domain)
            self.hits += 1
            return value

    def set(self, domain: str, response: DNSResponse) -> None:
        with self._lock:
            self._values[domain] = response
            self._values.move_to_end(domain)
            while len(self._values) > self.max_size:
                self._values.popitem(last=False)

    def stats(self) -> dict[str, int | float]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "size": len(self._values),
                "max_size": self.max_size,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": self.hits / total if total else 0.0,
            }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            domain: {
                "domain": response.domain,
                "result": response.result.value,
                "mx_records": [record.__dict__ for record in response.mx_records],
                "provider_id": response.provider_id,
                "error_msg": response.error_msg,
                "ttl": response.ttl,
                "latency_ms": response.latency_ms,
            }
            for domain, response in self._values.items()
        }
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def load(self, path: Path) -> bool:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError, OSError, json.JSONDecodeError:
            return False
        if not isinstance(value, dict):
            return False
        for domain, item in list(value.items())[: self.max_size]:
            try:
                response = DNSResponse(
                    domain=item["domain"],
                    result=DNSResult(item["result"]),
                    mx_records=tuple(MXRecord(**record) for record in item.get("mx_records", [])),
                    provider_id=item.get("provider_id"),
                    error_msg=item.get("error_msg"),
                    ttl=item.get("ttl"),
                    latency_ms=item.get("latency_ms"),
                )
            except KeyError, TypeError, ValueError:
                continue
            self.set(domain, response)
        return True


class DNSService:
    def __init__(
        self,
        *,
        mode: DNSMode = DNSMode.OFFLINE,
        cache: DNSCache | None = None,
        provider_catalog: ProviderCatalog | None = None,
        sync_resolver: object | None = None,
        async_resolver: object | None = None,
        timeout: float = 5.0,
        retry_count: int = 3,
        circuit_threshold: int = 10,
        circuit_timeout: float = 30.0,
        concurrency: int = 25,
    ):
        self.mode = mode
        self.cache = cache or DNSCache()
        self.provider_catalog = provider_catalog or ProviderCatalog()
        self.sync_resolver = sync_resolver
        self.async_resolver = async_resolver
        self.timeout = timeout
        self.retry_count = retry_count
        self.circuit_threshold = max(1, circuit_threshold)
        self.circuit_timeout = circuit_timeout
        self.concurrency = concurrency
        self.total_lookups = 0
        self.resolved_ok = 0
        self.error_counts: dict[str, int] = {}
        self._failures = 0
        self._last_failure_time = 0.0

    def _record_error(self, result: DNSResult) -> None:
        self.error_counts[result.value] = self.error_counts.get(result.value, 0) + 1

    @staticmethod
    def _exception_result(error: Exception) -> DNSResult:
        if isinstance(error, (TimeoutError, asyncio.TimeoutError, socket.timeout)):
            return DNSResult.TIMEOUT
        if hasattr(error, "args") and error.args and isinstance(error.args[0], int):
            if aiodns is not None:
                codes = {
                    aiodns.error.ARES_ENODATA: DNSResult.NO_MX,
                    aiodns.error.ARES_EFORMERR: DNSResult.FORMAT_ERROR,
                    aiodns.error.ARES_ESERVFAIL: DNSResult.SERVER_FAILURE,
                    aiodns.error.ARES_ENOTFOUND: DNSResult.NXDOMAIN,
                    aiodns.error.ARES_ENOTIMP: DNSResult.NOT_IMPLEMENTED,
                    aiodns.error.ARES_EREFUSED: DNSResult.REFUSED,
                    aiodns.error.ARES_ECONNREFUSED: DNSResult.CONNECTION_ERROR,
                    aiodns.error.ARES_ETIMEOUT: DNSResult.TIMEOUT,
                }
                return codes.get(error.args[0], DNSResult.GENERIC_ERROR)
            return {1: DNSResult.NO_MX, 2: DNSResult.FORMAT_ERROR}.get(
                error.args[0], DNSResult.GENERIC_ERROR
            )
        if isinstance(error, (ConnectionError, OSError)):
            return DNSResult.CONNECTION_ERROR
        if dns is not None:
            if isinstance(error, dns.resolver.NXDOMAIN):
                return DNSResult.NXDOMAIN
            if isinstance(error, dns.resolver.NoAnswer):
                return DNSResult.NO_MX
            if isinstance(error, dns.resolver.Timeout):
                return DNSResult.TIMEOUT
        return DNSResult.GENERIC_ERROR

    def _circuit_is_open(self) -> bool:
        if self._failures < self.circuit_threshold:
            return False
        if time.monotonic() - self._last_failure_time >= self.circuit_timeout:
            self._failures = self.circuit_threshold - 1
            return False
        return True

    @staticmethod
    def _should_cache(response: DNSResponse) -> bool:
        return response.result not in {
            DNSResult.TIMEOUT,
            DNSResult.CONNECTION_ERROR,
            DNSResult.SERVER_FAILURE,
            DNSResult.CIRCUIT_BROKEN,
            DNSResult.GENERIC_ERROR,
        }

    def _resolve_answer(self, domain: str, answers: Iterable[object]) -> DNSResponse:
        records: list[MXRecord] = []
        for answer in answers:
            host = str(getattr(answer, "exchange", getattr(answer, "host", ""))).lower().rstrip(".")
            priority = int(getattr(answer, "preference", getattr(answer, "priority", 0)))
            records.append(MXRecord(priority, host, self.provider_catalog.hosting_from_mx(host)))
        records.sort(key=lambda item: item.priority)
        provider_id = next((record.provider_id for record in records if record.provider_id), None)
        if provider_id is None and records:
            provider_id = "self_hosted"
        return DNSResponse(
            domain, DNSResult.OK if records else DNSResult.NO_MX, tuple(records), provider_id
        )

    def resolve_one_sync(self, domain: str, resolver: object | None = None) -> DNSResponse:
        cached = self.cache.get(domain)
        if cached is not None:
            return cached
        self.total_lookups += 1
        started = time.monotonic()
        resolver = resolver or self.sync_resolver
        if resolver is None and dns is not None:
            resolver = dns.resolver.Resolver()
            resolver.timeout = self.timeout
            resolver.lifetime = self.timeout
        if resolver is None:
            response = DNSResponse(domain, DNSResult.OFFLINE, error_msg="DNS is disabled")
        elif self._circuit_is_open():
            response = DNSResponse(
                domain, DNSResult.CIRCUIT_BROKEN, error_msg="DNS circuit is open"
            )
        else:
            response = None
            error: Exception | None = None
            for _attempt in range(self.retry_count):
                try:
                    response = self._resolve_answer(domain, resolver.resolve(domain, "MX"))
                    if response.result is DNSResult.OK:
                        self.resolved_ok += 1
                        self._failures = 0
                        self._last_failure_time = 0.0
                    break
                except Exception as caught:  # external resolver boundary
                    error = caught
            if response is None:
                result = self._exception_result(error or RuntimeError("DNS lookup failed"))
                self._record_error(result)
                self._failures += 1
                self._last_failure_time = time.monotonic()
                response = DNSResponse(domain, result, error_msg=str(error))
        response = DNSResponse(
            response.domain,
            response.result,
            response.mx_records,
            response.provider_id,
            response.error_msg,
            response.ttl,
            int((time.monotonic() - started) * 1000),
        )
        if self._should_cache(response):
            self.cache.set(domain, response)
        return response

    async def resolve_one_async(self, domain: str, resolver: object | None = None) -> DNSResponse:
        cached = self.cache.get(domain)
        if cached is not None:
            return cached
        self.total_lookups += 1
        resolver = resolver or self.async_resolver
        if resolver is None and aiodns is not None:
            resolver = aiodns.DNSResolver(timeout=self.timeout)
        if resolver is None:
            response = DNSResponse(domain, DNSResult.OFFLINE, error_msg="DNS is disabled")
        elif self._circuit_is_open():
            response = DNSResponse(
                domain, DNSResult.CIRCUIT_BROKEN, error_msg="DNS circuit is open"
            )
        else:
            response = None
            error: Exception | None = None
            for _attempt in range(self.retry_count):
                try:
                    answers = await resolver.query(domain, "MX")
                    response = self._resolve_answer(domain, answers)
                    if response.result is DNSResult.OK:
                        self.resolved_ok += 1
                        self._failures = 0
                        self._last_failure_time = 0.0
                    break
                except Exception as caught:  # external resolver boundary
                    error = caught
            if response is None:
                result = self._exception_result(error or RuntimeError("DNS lookup failed"))
                self._record_error(result)
                self._failures += 1
                self._last_failure_time = time.monotonic()
                response = DNSResponse(domain, result, error_msg=str(error))
        if self._should_cache(response):
            self.cache.set(domain, response)
        return response

    def resolve_batch_sync(self, domains: Iterable[str]) -> list[DNSResponse]:
        return [self.resolve_one_sync(domain) for domain in domains]

    async def resolve_batch_async(self, domains: Iterable[str]) -> list[DNSResponse]:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def resolve(domain: str) -> DNSResponse:
            async with semaphore:
                return await self.resolve_one_async(domain)

        return await asyncio.gather(*(resolve(domain) for domain in domains))

    def preflight_check(self, test_domain: str = "example.com") -> bool:
        """Check the configured DNS boundary without forcing network use."""

        if self.mode is DNSMode.OFFLINE:
            return False
        response = self.resolve_one_sync(test_domain)
        return response.result in {DNSResult.OK, DNSResult.NXDOMAIN, DNSResult.NO_MX}
