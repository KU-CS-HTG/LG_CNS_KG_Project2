"""Bounded original acquisition, exact bytes and atomic content-addressed promotion (A-1)."""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import time
from urllib.parse import unquote, urljoin, urlsplit
import uuid
from zipfile import ZipFile, BadZipFile

import requests

from src.ingest.document_catalog import dumps, now, safe_url


class AcquisitionError(Exception):
    def __init__(self, code, *, transient=False, retry_after=0):
        super().__init__(code)
        self.code, self.transient = code, transient
        self.retry_after = retry_after


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 64 * 1024 * 1024
    attempts: int = 3
    redirects: int = 3
    seconds: int = 90

    def checked(self):
        if (type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 64 * 1024 * 1024
                or type(self.attempts) is not int or not 1 <= self.attempts <= 3
                or type(self.redirects) is not int or not 0 <= self.redirects <= 3
                or type(self.seconds) is not int or not 1 <= self.seconds <= 90):
            raise ValueError("acquisition_limits_out_of_bounds")
        return self


def assess(path):
    """Structure/page count only; no page text, OCR, clauses, tables or labels extracted."""
    with path.open("rb") as stream:
        header = stream.read(1024)
        stream.seek(max(0, path.stat().st_size - 2048))
        tail = stream.read()
    quality = {"valid_pdf": False, "page_count": None, "text_availability": "not_assessed",
               "ocr": "not_assessed", "extraction": "deferred_A2", "locations": None}
    if header.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        raise AcquisitionError("unexpected_html")
    if header.startswith(b"%PDF-"):
        if b"%%EOF" not in tail:
            raise AcquisitionError("truncated_pdf")
        try:
            from pypdf import PdfReader
            reader = PdfReader(path, strict=True)
            try:
                if reader.is_encrypted:
                    raise AcquisitionError("encrypted_pdf_not_assessed")
                count = reader.get_num_pages()
                if not 1 <= count <= 2000:
                    raise AcquisitionError("page_count_out_of_bounds")
            finally:
                reader.close()
        except AcquisitionError:
            raise
        except Exception:
            raise AcquisitionError("invalid_pdf_structure") from None
        quality.update(valid_pdf=True, page_count=count, integrity="pypdf_strict_structure_and_EOF")
        return "pdf", quality
    if header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        quality["integrity"] = "OLE_signature_only_not_full_format_validation"
        return "ole", quality
    if header.startswith(b"PK\x03\x04"):
        try:
            with ZipFile(path) as archive:
                names = set(archive.namelist())
                kind = "hwpx" if "Contents/content.hpf" in names else "docx" if "word/document.xml" in names else "zip"
        except BadZipFile:
            raise AcquisitionError("invalid_archive") from None
        quality["integrity"] = "zip_directory_only_not_document_validation"
        return kind, quality
    raise AcquisitionError("unsupported_or_error_content")


def _download(source, path, session, limits):
    entry = source["payload"]
    url = entry.get("attachment_url")
    if not url:
        raise AcquisitionError("attachment_unresolved")
    if (entry["facts"]["identity_evidence"]["verification"] != "official_evidence_verified"
            or entry["attachment_evidence"]["verification"] != "official_evidence_verified"):
        raise AcquisitionError("source_relationship_unverified")
    allowed = set(entry.get("allowed_hosts", []))
    started, seen, chain = time.monotonic(), set(), []
    for n in range(limits.redirects + 1):
        safe_url(url)
        if urlsplit(url).hostname not in allowed:
            raise AcquisitionError("unverified_attachment_host")
        if url in seen:
            raise AcquisitionError("redirect_loop")
        seen.add(url)
        with session.get(url, timeout=(10, 20), stream=True, allow_redirects=False,
                         headers={"Accept-Encoding": "identity", "User-Agent": "LG-CNS-KG-A1/1.0"}) as response:
            chain.append({"url": url, "http": response.status_code})
            if response.status_code in (301, 302, 303, 307, 308):
                if n == limits.redirects:
                    raise AcquisitionError("redirect_limit")
                target = response.headers.get("Location")
                if not target:
                    raise AcquisitionError("redirect_missing_location")
                url = urljoin(url, target)
                continue
            if response.status_code != 200:
                retry = response.headers.get("Retry-After", "")
                raise AcquisitionError("http_" + str(response.status_code),
                                       transient=response.status_code in (408, 429, 500, 502, 503, 504),
                                       retry_after=min(int(retry), 5) if retry.isdigit() else 0)
            if response.headers.get("Content-Encoding", "identity").lower() not in ("identity", ""):
                raise AcquisitionError("unexpected_content_encoding")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > limits.max_bytes):
                raise AcquisitionError("invalid_or_excessive_content_length")
            count = 0
            with path.open("xb") as output:
                for part in response.iter_content(65536):
                    if time.monotonic() - started > limits.seconds:
                        raise AcquisitionError("download_time_limit", transient=True)
                    count += len(part)
                    if count > limits.max_bytes:
                        raise AcquisitionError("file_size_limit")
                    output.write(part)
                output.flush()
                os.fsync(output.fileno())
            if not count or (length is not None and count != int(length)):
                raise AcquisitionError("truncated_download", transient=True)
            media = response.headers.get("Content-Type", "").split(";")[0].lower()
            if media in ("text/html", "application/xhtml+xml"):
                raise AcquisitionError("unexpected_html")
            disposition = response.headers.get("Content-Disposition", "")[:1000]
            filename = entry.get("original_filename") or Path(unquote(urlsplit(url).path)).name
            final_url = safe_url(getattr(response, "url", url))
            if urlsplit(final_url).hostname not in allowed:
                raise AcquisitionError("unverified_attachment_host")
            return {"final_url": final_url, "redirect_chain": chain, "media_type": media, "original_filename": filename,
                    "content_disposition": disposition, "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"), "size": count}
    raise AcquisitionError("redirect_limit")


def acquire(catalog, source_id, *, limits=Limits(), session=None, sleep=time.sleep, local_file=None):
    """One explicitly registered source, at most three requests attempts; no implicit crawl.

    Each retry has a durable attempt. Failed refresh never deletes valid observations.
    Local originals have unverified-local assurance, even for a verified posting.
    """
    limits = limits.checked()
    source = catalog.source(source_id)
    local = Path(local_file).resolve() if local_file else None
    partials = catalog.paths.originals / ".partial"
    partials.mkdir(parents=True, exist_ok=True)
    owns_session = session is None
    session = session or requests.Session()
    try:
        for n in range(1 if local else limits.attempts):
            attempt_id = catalog.start_attempt(source, "local_supplied" if local else "official_http")
            temporary = partials / (attempt_id + ".part")
            detail = {"source_id": source_id, "collected_at": now(), "assurance": "unverified_local" if local else "official_endpoint_observed"}
            transient, retry_after = False, 0
            try:
                if local:
                    if local.stat().st_size > limits.max_bytes:
                        raise AcquisitionError("file_size_limit")
                    size = 0
                    with local.open("rb") as src, temporary.open("xb") as dest:
                        while part := src.read(65536):
                            size += len(part)
                            if size > limits.max_bytes:
                                raise AcquisitionError("file_size_limit")
                            dest.write(part)
                        dest.flush(); os.fsync(dest.fileno())
                    detail.update(original_filename=local.name, supplied_path=str(local), media_type="not_observed", size=size,
                                  final_url=None, official_content_identity="unverified")
                else:
                    detail.update(_download(source, temporary, session, limits))
                kind, quality = assess(temporary)
                if source["payload"].get("expected_format") == "pdf" and kind != "pdf":
                    # Preserve the actual original format, but explicitly flag mismatch.
                    quality["expected_format_mismatch"] = "expected_pdf_observed_" + kind
                with temporary.open("rb") as received:
                    sha = hashlib.file_digest(received, "sha256").hexdigest()
                relative = Path(sha[:2]) / (sha + "." + kind)
                target = catalog.paths.originals / relative
                artifact = {"sha256": sha, "relative_path": relative.as_posix(), "size": temporary.stat().st_size,
                            "format": kind, "quality": quality}
                def promote():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        with target.open("rb") as existing:
                            if hashlib.file_digest(existing, "sha256").hexdigest() != sha:
                                raise AcquisitionError("existing_artifact_integrity_mismatch")
                    else:
                        os.replace(temporary, target)
                result = catalog.finish_attempt(attempt_id, "acquired_pdf" if kind == "pdf" else "acquired_non_pdf",
                                                detail, source, artifact, promote)
            except BaseException as exc:
                code = exc.code if isinstance(exc, AcquisitionError) else (
                    "timeout" if isinstance(exc, requests.Timeout) else "connection_failure" if isinstance(exc, requests.ConnectionError)
                    else "truncated_transport" if isinstance(exc, requests.exceptions.ChunkedEncodingError)
                    else "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "io_or_validation_failure")
                transient = getattr(exc, "transient", False) or code in ("timeout", "connection_failure", "truncated_transport")
                retry_after = getattr(exc, "retry_after", 0)
                result = catalog.finish_attempt(attempt_id, "failed", {**detail, "assurance": "not_acquired", "failure": code}, source)
                if not isinstance(exc, Exception):
                    raise
            finally:
                temporary.unlink(missing_ok=True)  # Only this attempt's own temporary file.
            # Catalog is authoritative if diagnostics serialization fails after commit.
            catalog.paths.diagnostics.mkdir(parents=True, exist_ok=True)
            diagnostic = catalog.paths.diagnostics / (attempt_id + ".json")
            aux = diagnostic.with_suffix(".tmp")
            aux.write_text(dumps(result), encoding="utf-8")
            os.replace(aux, diagnostic)
            if result["status"] != "failed" or not transient or n + 1 == limits.attempts:
                return result
            sleep(max(min(2 ** n, 4), retry_after))
    finally:
        if owns_session:
            session.close()
