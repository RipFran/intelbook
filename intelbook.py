#!/usr/bin/env python3
"""
intelbook.py

IntelX Phonebook CLI:
- Queries Phonebook for emails, domains, and/or URLs for one or more domains.
- Writes per-domain outputs and merged outputs.
- Implements correct polling logic based on IntelX Phonebook status codes:
  0: Success with results (keep polling/paging)
  1: Finished, no future results (stop; may still include records)
  2: Search ID not found (error)
  3: No results yet available, keep trying

Docs:
- IntelX Search API v5 (September 30, 2022)
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse

import requests
from requests import Response
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table


# -----------------------------
# IntelX Phonebook constants
# -----------------------------
TARGET_DOMAIN = 1
TARGET_EMAIL = 2
TARGET_URL = 3

TYPE_TO_TARGET = {
    "domains": TARGET_DOMAIN,
    "emails": TARGET_EMAIL,
    "urls": TARGET_URL,
}

# Some IntelX deployments return URL selectors with different selectortype values.
# We keep this flexible by best-effort parsing; still, these are common:
SELECTORTYPE_EMAIL = 1
SELECTORTYPE_DOMAIN = 2
SELECTORTYPE_URL = 3
SELECTORTYPE_URL_QUERY = 23
URL_SELECTOR_TYPES = {SELECTORTYPE_URL, SELECTORTYPE_URL_QUERY}


# -----------------------------
# Logging
# -----------------------------
def configure_logging(verbosity: int) -> logging.Logger:
    level = logging.WARNING if verbosity <= 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_time=True, show_level=True, show_path=False)],
    )
    return logging.getLogger("intelbook")


# -----------------------------
# Utilities
# -----------------------------
def normalize_domain(term: str) -> str:
    s = term.strip()
    if not s:
        return ""
    if "://" in s:
        parsed = urlparse(s)
        host = parsed.netloc.strip()
        if "@" in host:
            host = host.split("@", 1)[1]
        if ":" in host:
            host = host.split(":", 1)[0]
        s = host
    s = s.strip().lower()
    s = s[:-1] if s.endswith(".") else s
    return s


def sanitize_for_dirname(s: str) -> str:
    s = s.strip()
    if not s:
        return "unknown"
    s = s.replace("*", "_wildcard_")
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s)
    s = s.strip("._-")
    return s or "unknown"


def read_domains_from_file(path: str) -> List[str]:
    domains: List[str] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            d = normalize_domain(line)
            if d:
                domains.append(d)
    return domains


def write_sorted_unique(path: str, values: Iterable[str]) -> int:
    uniq = sorted({v.strip() for v in values if v and v.strip()})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for v in uniq:
            f.write(v + "\n")
    return len(uniq)


def safe_body(resp: Response, max_len: int = 2000) -> str:
    try:
        text = resp.text
    except Exception:
        return "<unreadable body>"
    text = text.strip()
    return text if len(text) <= max_len else (text[:max_len] + "...(truncated)")


# -----------------------------
# Rate limiter
# -----------------------------
@dataclass
class RateLimiter:
    rps: float
    _next_allowed: float = 0.0

    def wait(self) -> None:
        if self.rps <= 0:
            return
        now = time.monotonic()
        if now < self._next_allowed:
            time.sleep(self._next_allowed - now)
        self._next_allowed = max(self._next_allowed, now) + (1.0 / self.rps)


# -----------------------------
# IntelX Client
# -----------------------------
class IntelXClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        user_agent: str,
        rps: float,
        timeout_s: float,
        logger: logging.Logger,
        verify_tls: bool = True,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent.strip()
        self.timeout_s = timeout_s
        self.verify_tls = verify_tls
        self.log = logger

        if not self.api_key:
            raise ValueError("API key is empty.")
        if not self.user_agent:
            raise ValueError("User-Agent is empty (IntelX requires self-identification).")

        self.session = requests.Session()
        self.session.headers.update(
            {
                "x-key": self.api_key,
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            }
        )
        self.rl = RateLimiter(rps=rps)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        max_retries: int = 5,
    ) -> Response:
        url = f"{self.base_url}{path}"
        attempt = 0
        backoff = 1.0

        while True:
            attempt += 1
            self.rl.wait()
            try:
                resp = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_body,
                    timeout=self.timeout_s,
                    verify=self.verify_tls,
                )
            except requests.RequestException as e:
                if attempt >= max_retries:
                    raise
                self.log.warning(f"Network error calling {path}: {e}. Retrying in {backoff:.1f}s...")
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 20.0)
                continue

            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt >= max_retries:
                    return resp
                retry_after = resp.headers.get("Retry-After")
                wait_s = float(retry_after) if (retry_after and retry_after.isdigit()) else backoff
                self.log.warning(f"HTTP {resp.status_code} from {path}. Retrying in {wait_s:.1f}s...")
                time.sleep(wait_s)
                backoff = min(backoff * 2.0, 20.0)
                continue

            return resp

    def terminate_search(self, search_id: str) -> None:
        # Same endpoint is used for normal searches and phonebook lookups.
        try:
            self._request("GET", "/intelligent/search/terminate", params={"id": search_id})
        except Exception:
            # Termination is best-effort; don't fail the whole run.
            self.log.debug(f"Failed to terminate search id {search_id}", exc_info=True)

    def phonebook_search(
        self,
        term: str,
        target: int,
        maxresults: int,
        timeout_s: int,
        terminate: Optional[List[Optional[str]]] = None,
    ) -> str:
        payload: Dict[str, Any] = {
            "term": term,
            "buckets": [],
            "lookuplevel": 0,
            "maxresults": maxresults,
            "timeout": timeout_s,
            "datefrom": "",
            "dateto": "",
            "sort": 2,
            "media": 0,
            "terminate": terminate or [],
            "target": target,
        }

        resp = self._request("POST", "/phonebook/search", json_body=payload)
        if resp.status_code != 200:
            raise RuntimeError(f"Phonebook search failed (HTTP {resp.status_code}): {safe_body(resp)}")

        try:
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"Phonebook search returned non-JSON: {e} | body={safe_body(resp)}")

        status = int(data.get("status", -1))
        if status != 0:
            raise RuntimeError(f"Phonebook search rejected (status={status}): {json.dumps(data, ensure_ascii=False)}")

        search_id = str(data.get("id", "")).strip()
        if not search_id:
            raise RuntimeError(f"Phonebook search did not return an id: {json.dumps(data, ensure_ascii=False)}")
        return search_id

    @staticmethod
    def _extract_records_from_response(payload: Any) -> Tuple[int, List[Any], Dict[str, Any]]:
        """
        Returns: (status, records_list, normalized_dict_for_debug)

        PhonebookSearchResult is documented as a JSON structure with a `status` field and result data.
        In practice, different instances can use different keys for the array. We support:
        - records
        - selectors
        - results
        - items
        """
        if isinstance(payload, dict):
            status = int(payload.get("status", -1))

            for key in ("records", "selectors", "results", "items", "data"):
                v = payload.get(key)
                if isinstance(v, list):
                    return status, v, payload

            # Some instances may return the list directly in an unexpected key; fallback to empty list.
            return status, [], payload

        if isinstance(payload, list):
            # If the instance returns a raw array, status is implicitly "success with results".
            return 0, payload, {"status": 0, "records": payload}

        return -1, [], {"status": -1, "raw": payload}

    def phonebook_fetch_all(
        self,
        search_id: str,
        limit: int,
        poll_interval_s: float,
        max_wait_s: float,
        debug_dump_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetches all results from Phonebook by polling & paging until status==1.
        - status 3: keep trying (no results yet)
        - status 0: success with results (may continue)
        - status 1: finished (may still include records)
        - status 2: search ID not found (error)

        We page using offset when we actually receive records.
        """
        start = time.monotonic()
        offset = 0

        all_records: List[Dict[str, Any]] = []
        seen: Set[str] = set()

        last_status: Optional[int] = None
        empty_pages_in_a_row = 0

        while True:
            if (time.monotonic() - start) > max_wait_s:
                raise TimeoutError(f"Phonebook polling exceeded max_wait_s={max_wait_s}s")

            params = {"id": search_id, "limit": limit, "offset": offset}
            resp = self._request("GET", "/phonebook/search/result", params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"Phonebook result fetch failed (HTTP {resp.status_code}): {safe_body(resp)}")

            try:
                payload = resp.json()
            except Exception as e:
                raise RuntimeError(f"Phonebook result returned non-JSON: {e} | body={safe_body(resp)}")

            status, raw_records, normalized = self._extract_records_from_response(payload)
            last_status = status

            # Optional debug dump: always write the *last* response for this query type.
            if debug_dump_path:
                os.makedirs(os.path.dirname(debug_dump_path), exist_ok=True)
                with open(debug_dump_path, "w", encoding="utf-8") as f:
                    json.dump(normalized, f, ensure_ascii=False, indent=2)

            # Handle status codes as per docs
            if status == 2:
                raise RuntimeError("Phonebook status=2 (Search ID not found)")

            # Normalize record entries to dicts (best-effort)
            batch: List[Dict[str, Any]] = []
            for r in raw_records:
                if isinstance(r, dict):
                    batch.append(r)
                elif isinstance(r, str):
                    batch.append({"selector": r})
                else:
                    batch.append({"raw": r})

            # De-dup
            new_count = 0
            for r in batch:
                fp = json.dumps(r, sort_keys=True, ensure_ascii=False)
                if fp in seen:
                    continue
                seen.add(fp)
                all_records.append(r)
                new_count += 1

            # Paging / polling logic:
            if len(batch) == 0:
                empty_pages_in_a_row += 1
            else:
                empty_pages_in_a_row = 0
                offset += len(batch)

            # If finished, stop (even if records were included in this final response)
            if status == 1:
                break

            # If status==3, it explicitly means "keep trying".
            # If status==0 but we got an empty page repeatedly, keep polling (backend may be building results).
            time.sleep(poll_interval_s)

        return all_records


# -----------------------------
# Selector extraction
# -----------------------------
def extract_selectors(records: List[Dict[str, Any]]) -> Tuple[Set[str], Set[str], Set[str]]:
    emails: Set[str] = set()
    domains: Set[str] = set()
    urls: Set[str] = set()

    def add_email(v: str) -> None:
        v = v.strip()
        if v and "@" in v and " " not in v:
            emails.add(v)

    def add_domain(v: str) -> None:
        v = v.strip().lower().rstrip(".")
        if v and " " not in v and "/" not in v and "@" not in v:
            domains.add(v)

    def add_url(v: str) -> None:
        v = v.strip()
        if not v:
            return
        if "://" in v or v.startswith("ftp://") or v.startswith("magnet:"):
            urls.add(v)

    for r in records:
        st = r.get("selectortype")
        selector = r.get("selector") or r.get("value") or r.get("term")

        if isinstance(selector, str):
            selector = html.unescape(selector).strip()

        if isinstance(st, int) and isinstance(selector, str):
            if st == SELECTORTYPE_EMAIL:
                add_email(selector)
                continue
            if st == SELECTORTYPE_DOMAIN:
                add_domain(selector)
                continue
            if st in URL_SELECTOR_TYPES:
                add_url(selector)
                continue

        # Best-effort classification
        if isinstance(selector, str):
            if "@" in selector and " " not in selector:
                add_email(selector)
            elif "://" in selector or selector.startswith("ftp://") or selector.startswith("magnet:"):
                add_url(selector)
            elif "/" not in selector and " " not in selector:
                add_domain(selector)

        # Also scan any other string fields
        for v in r.values():
            if not isinstance(v, str):
                continue
            v = html.unescape(v).strip()
            if not v:
                continue
            if "@" in v and " " not in v:
                add_email(v)
            elif "://" in v or v.startswith("ftp://") or v.startswith("magnet:"):
                add_url(v)
            elif "/" not in v and " " not in v:
                add_domain(v)

    return emails, domains, urls


# -----------------------------
# CLI
# -----------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="intelbook.py",
        description="Query IntelX Phonebook for URLs, domains, and email addresses for one or more domains.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    p.add_argument(
        "--api-key",
        dest="api_key",
        default=os.environ.get("INTELX_API_KEY", ""),
        help="IntelX API key (UUID). You can also set INTELX_API_KEY env var.",
    )
    p.add_argument("--base-url", default="https://2.intelx.io", help="IntelX API base URL (paid commonly uses 2.intelx.io).")
    p.add_argument("--user-agent", default="intelbook/1.1", help="User-Agent header (required by IntelX).")

    p.add_argument("--input", required=True, help="Input domain OR a file containing one domain per line.")
    p.add_argument("--types", default="emails,domains,urls", help="Comma-separated list: emails, domains, urls.")
    p.add_argument("--output-dir", default="output", help="Root output directory.")

    p.add_argument(
        "--term-mode",
        choices=["wildcard", "exact", "both"],
        default="wildcard",
        help="How to build the selector term for Phonebook. wildcard => *.domain, exact => domain, both => both terms merged.",
    )

    p.add_argument("--maxresults", type=int, default=10000, help="Phonebook maxresults parameter per search.")
    p.add_argument("--result-limit", type=int, default=200, help="Phonebook result page size (limit parameter).")
    p.add_argument("--pb-timeout", type=int, default=20, help="Phonebook search timeout parameter (seconds).")
    p.add_argument("--http-timeout", type=float, default=30.0, help="HTTP request timeout (seconds).")
    p.add_argument("--rps", type=float, default=1.0, help="Max requests per second (aligns with IntelX default guidance).")
    p.add_argument("--poll-interval", type=float, default=1.0, help="Polling interval (seconds).")
    p.add_argument("--max-wait", type=float, default=180.0, help="Max seconds to wait for a single Phonebook search to finish.")

    p.add_argument("--no-tls-verify", action="store_true", help="Disable TLS verification (not recommended).")
    p.add_argument("--json-debug", action="store_true", help="Write raw last JSON response per query type for debugging.")
    p.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity (-v, -vv).")

    return p


def parse_types(types_csv: str) -> List[str]:
    items = [t.strip().lower() for t in types_csv.split(",") if t.strip()]
    allowed = {"emails", "domains", "urls"}
    bad = [t for t in items if t not in allowed]
    if bad:
        raise ValueError(f"Invalid --types value(s): {', '.join(bad)}. Allowed: emails, domains, urls.")
    order = ["emails", "domains", "urls"]
    return [t for t in order if t in set(items)]


def build_terms(domain: str, mode: str) -> List[str]:
    domain = normalize_domain(domain)
    if not domain:
        return []
    wildcard = domain if domain.startswith("*.") else f"*.{domain}"
    if mode == "wildcard":
        return [wildcard]
    if mode == "exact":
        return [domain]
    # both
    if wildcard == domain:
        return [domain]
    return [domain, wildcard]


def main() -> int:
    args = build_arg_parser().parse_args()
    log = configure_logging(args.verbose)
    console = Console()

    api_key = args.api_key.strip()
    if not api_key:
        console.print("[bold red]Error:[/bold red] Missing --api-key or INTELX_API_KEY environment variable.")
        return 2

    try:
        wanted_types = parse_types(args.types)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        return 2

    # Load domains
    input_value = args.input.strip()
    if os.path.isfile(input_value):
        domains = read_domains_from_file(input_value)
        if not domains:
            console.print("[bold red]Error:[/bold red] Input file contained no valid domains.")
            return 2
    else:
        d = normalize_domain(input_value)
        if not d:
            console.print("[bold red]Error:[/bold red] Input domain is empty or invalid.")
            return 2
        domains = [d]

    output_root = os.path.abspath(args.output_dir)
    os.makedirs(output_root, exist_ok=True)

    client = IntelXClient(
        api_key=api_key,
        base_url=args.base_url,
        user_agent=args.user_agent,
        rps=args.rps,
        timeout_s=args.http_timeout,
        logger=log,
        verify_tls=not args.no_tls_verify,
    )

    all_emails: Set[str] = set()
    all_domains: Set[str] = set()
    all_urls: Set[str] = set()

    console.rule("[bold]IntelX Phonebook Extraction[/bold]")
    console.print(f"Base URL      : {args.base_url}")
    console.print(f"Domains       : {len(domains)}")
    console.print(f"Types         : {', '.join(wanted_types)}")
    console.print(f"Term mode     : {args.term_mode}")
    console.print(f"Output dir    : {output_root}")
    console.print(f"Rate limit    : {args.rps:.2f} req/s")
    console.print("")

    start_ts = time.time()

    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Processing domains", total=len(domains))

        for domain in domains:
            console.rule(f"[bold]Domain:[/bold] {domain}")
            domain_dir = os.path.join(output_root, sanitize_for_dirname(domain))
            os.makedirs(domain_dir, exist_ok=True)

            domain_emails: Set[str] = set()
            domain_domains: Set[str] = set()
            domain_urls: Set[str] = set()

            terms = build_terms(domain, args.term_mode)
            if not terms:
                console.print("  [bold red]Skipping:[/bold red] could not build a valid term for this domain.")
                progress.update(task, advance=1)
                continue

            for out_type in wanted_types:
                target = TYPE_TO_TARGET[out_type]
                console.print(f"- Query type: [bold]{out_type}[/bold] (target={target})")

                combined_records: List[Dict[str, Any]] = []

                for term in terms:
                    console.print(f"  Starting Phonebook search for term: {term}")

                    search_id = ""
                    try:
                        search_id = client.phonebook_search(
                            term=term,
                            target=target,
                            maxresults=args.maxresults,
                            timeout_s=args.pb_timeout,
                        )
                        console.print(f"  Search ID: {search_id}")
                    except Exception as e:
                        console.print(f"  [bold red]Failed[/bold red] to start search: {e}")
                        continue

                    debug_path = None
                    if args.json_debug:
                        debug_path = os.path.join(domain_dir, f"debug_last_{out_type}_{sanitize_for_dirname(term)}.json")

                    try:
                        records = client.phonebook_fetch_all(
                            search_id=search_id,
                            limit=args.result_limit,
                            poll_interval_s=args.poll_interval,
                            max_wait_s=args.max_wait,
                            debug_dump_path=debug_path,
                        )
                        combined_records.extend(records)
                        console.print(f"  Finished: fetched {len(records)} raw record(s)")
                    except TimeoutError as e:
                        console.print(f"  [bold yellow]Timeout[/bold yellow]: {e}")
                        if search_id:
                            client.terminate_search(search_id)
                            console.print("  Best-effort cleanup: search terminated.")
                    except Exception as e:
                        console.print(f"  [bold red]Failed[/bold red] to fetch results: {e}")
                        continue

                # Extract selectors from combined records for this type
                e_set, d_set, u_set = extract_selectors(combined_records)

                if out_type == "emails":
                    domain_emails |= e_set
                elif out_type == "domains":
                    domain_domains |= d_set
                elif out_type == "urls":
                    domain_urls |= u_set

                console.print(f"  Post-processed totals for this query: emails={len(e_set)}, domains={len(d_set)}, urls={len(u_set)}")

            # Write per-domain files
            written: Dict[str, int] = {}
            if "emails" in wanted_types:
                written["emails"] = write_sorted_unique(os.path.join(domain_dir, "emails.txt"), domain_emails)
            if "domains" in wanted_types:
                written["domains"] = write_sorted_unique(os.path.join(domain_dir, "domains.txt"), domain_domains)
            if "urls" in wanted_types:
                written["urls"] = write_sorted_unique(os.path.join(domain_dir, "urls.txt"), domain_urls)

            all_emails |= domain_emails
            all_domains |= domain_domains
            all_urls |= domain_urls

            t = Table(title="Domain summary", show_header=True, header_style="bold")
            t.add_column("Type")
            t.add_column("Count", justify="right")
            for k in ["emails", "domains", "urls"]:
                if k in wanted_types:
                    t.add_row(k, str(written.get(k, 0)))
            console.print(t)

            progress.update(task, advance=1)

    console.rule("[bold]Writing merged outputs[/bold]")
    if "emails" in wanted_types:
        write_sorted_unique(os.path.join(output_root, "emails_all.txt"), all_emails)
        console.print(f"- emails_all.txt  : {len(all_emails)}")
    if "domains" in wanted_types:
        write_sorted_unique(os.path.join(output_root, "domains_all.txt"), all_domains)
        console.print(f"- domains_all.txt : {len(all_domains)}")
    if "urls" in wanted_types:
        write_sorted_unique(os.path.join(output_root, "urls_all.txt"), all_urls)
        console.print(f"- urls_all.txt    : {len(all_urls)}")

    elapsed = time.time() - start_ts
    console.rule("[bold]Completed[/bold]")
    summary = Table(show_header=False)
    summary.add_row("Domains processed", str(len(domains)))
    summary.add_row("Elapsed time", f"{elapsed:.1f}s")
    summary.add_row("Output directory", output_root)
    console.print(summary)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
