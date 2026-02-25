from __future__ import annotations

import datetime as dt
from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from typing import List

from .base import Base


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Canonical IČO (usually 8 digits, but we store whatever upstream provides)
    ico: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # Normalized IČO used for fast matching (digits-only, left padded to 8 where applicable)
    ico_norm: Mapped[str | None] = mapped_column(String(16), unique=True, index=True, nullable=True)
    dic: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    legal_form: Mapped[str | None] = mapped_column(String(256), nullable=True)
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    street: Mapped[str | None] = mapped_column(String(256), nullable=True)
    street_number: Mapped[str | None] = mapped_column(String(32), nullable=True)          # cislo popisne
    orientation_number: Mapped[str | None] = mapped_column(String(32), nullable=True)     # cislo orientacni
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    zip_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_vat_payer: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ares_last_sync: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    pending_ares: Mapped[bool] = mapped_column(Boolean, default=False)

    documents: Mapped[List["Document"]] = relationship(back_populates="supplier")  # type: ignore[name-defined]


class DocumentFile(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    original_name: Mapped[str] = mapped_column(String(256))
    mime_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pages: Mapped[int] = mapped_column(Integer, default=1)
    current_path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), index=True)  # NEW/PROCESSED/QUARANTINE/DUPLICATE/ERROR
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    # one file can contain multiple documents (e.g., multiple receipts in one PDF)
    documents: Mapped[List["Document"]] = relationship(back_populates="file", cascade="all, delete-orphan")  # type: ignore[name-defined]


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id"), index=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)

    supplier_ico: Mapped[str | None] = mapped_column(String(16), index=True, nullable=True)
    doc_number: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    bank_account: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    issue_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    total_with_vat: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_without_vat: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_vat_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    vat_breakdown_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # invoice/receipt
    processing_profile: Mapped[str | None] = mapped_column(Text, nullable=True)
    # page range within the original file (1-based, inclusive)
    page_from: Mapped[int] = mapped_column(Integer, default=1)
    page_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="CZK")

    extraction_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    extraction_method: Mapped[str] = mapped_column(String(16), default="offline")  # offline/openai/manual
    # aggregated quality of chosen per-page text (0..1)
    document_text_quality: Mapped[float] = mapped_column(Float, default=0.0)
    openai_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    openai_raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reasons: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)

    file: Mapped[DocumentFile] = relationship(back_populates="documents")
    supplier: Mapped[Supplier | None] = relationship(back_populates="documents")
    items: Mapped[List["LineItem"]] = relationship(back_populates="document", cascade="all, delete-orphan")  # type: ignore[name-defined]

    __table_args__ = (
        Index("ix_documents_issue_date", "issue_date"),
        Index("ix_documents_file_page", "file_id", "page_from", "page_to"),
    )


class DocumentPageAudit(Base):
    __tablename__ = "document_page_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True)

    page_no: Mapped[int] = mapped_column(Integer)  # 1-based
    chosen_mode: Mapped[str] = mapped_column(String(16))  # embedded/ocr

    chosen_score: Mapped[float] = mapped_column(Float, default=0.0)
    embedded_score: Mapped[float] = mapped_column(Float, default=0.0)
    ocr_score: Mapped[float] = mapped_column(Float, default=0.0)

    embedded_len: Mapped[int] = mapped_column(Integer, default=0)
    ocr_len: Mapped[int] = mapped_column(Integer, default=0)
    ocr_conf: Mapped[float] = mapped_column(Float, default=0.0)
    token_groups: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("document_id", "page_no", name="uq_page_audit_doc_page"),
        Index("ix_page_audit_file_page", "file_id", "page_no"),
    )


class LineItem(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)

    name: Mapped[str] = mapped_column(String(512))
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_price_net: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_price_gross: Mapped[float | None] = mapped_column(Float, nullable=True)
    vat_rate: Mapped[float] = mapped_column(Float, default=0.0)
    line_total: Mapped[float] = mapped_column(Float, default=0.0)
    line_total_net: Mapped[float | None] = mapped_column(Float, nullable=True)
    line_total_gross: Mapped[float | None] = mapped_column(Float, nullable=True)
    vat_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    vat_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ean: Mapped[str | None] = mapped_column(String(64), nullable=True)
    item_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    document: Mapped[Document] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("document_id", "line_no", name="uq_items_doc_line"),
    )


class ImportJob(Base):
    __tablename__ = "import_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    processing_id_in: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)  # QUEUED/RUNNING/DONE/ERROR/DUPLICATE/QUARANTINE
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ServiceState(Base):
    __tablename__ = "service_state"

    singleton: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    running: Mapped[bool] = mapped_column(Boolean, default=False)
    last_success: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    queue_size: Mapped[int] = mapped_column(Integer, default=0)
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    # dashboard / observability
    inflight: Mapped[int] = mapped_column(Integer, default=0)                 # futures currently running
    max_workers: Mapped[int] = mapped_column(Integer, default=0)              # configured worker pool size
    current_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_phase: Mapped[str | None] = mapped_column(String(32), nullable=True)   # idle/scanning/dispatching/processing/shutdown
    current_progress: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0..100 best-effort
    heartbeat_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    stuck: Mapped[bool] = mapped_column(Boolean, default=False)
    stuck_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
