from __future__ import annotations

import datetime as dt
from typing import List

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from kajovospend.utils.time import utc_now_naive


class BaseProduction(DeclarativeBase):
    pass


class Supplier(BaseProduction):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ico: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    ico_norm: Mapped[str | None] = mapped_column(String(16), unique=True, index=True, nullable=True)
    dic: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    legal_form: Mapped[str | None] = mapped_column(String(256), nullable=True)
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    street: Mapped[str | None] = mapped_column(String(256), nullable=True)
    street_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    orientation_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    zip_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_vat_payer: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ares_last_sync: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    pending_ares: Mapped[bool] = mapped_column(Boolean, default=False)

    documents: Mapped[List["Document"]] = relationship(back_populates="supplier")  # type: ignore[name-defined]


class Document(BaseProduction):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # keep file_id nullable to decouple from working DB physical tables
    file_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)

    supplier_ico: Mapped[str | None] = mapped_column(String(16), index=True, nullable=True)
    doc_number: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    bank_account: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    issue_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    total_with_vat: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_without_vat: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_vat_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    vat_breakdown_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    processing_profile: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_from: Mapped[int] = mapped_column(Integer, default=1)
    page_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="CZK")

    extraction_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    extraction_method: Mapped[str] = mapped_column(String(16), default="offline")
    document_text_quality: Mapped[float] = mapped_column(Float, default=0.0)
    openai_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    openai_raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reasons: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utc_now_naive)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utc_now_naive, onupdate=utc_now_naive)

    supplier: Mapped[Supplier | None] = relationship(back_populates="documents")
    items: Mapped[List["LineItem"]] = relationship(back_populates="document", cascade="all, delete-orphan")  # type: ignore[name-defined]
    page_audits: Mapped[List["DocumentPageAudit"]] = relationship(back_populates="document", cascade="all, delete-orphan")  # type: ignore[name-defined]

    __table_args__ = (
        Index("ix_p_documents_issue_date", "issue_date"),
        Index("ix_p_documents_file_page", "file_id", "page_from", "page_to"),
    )


class LineItem(BaseProduction):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    id_item: Mapped[int | None] = mapped_column(Integer, unique=True, index=True, nullable=True)
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

    __table_args__ = (UniqueConstraint("document_id", "line_no", name="uq_p_items_doc_line"),)


class StandardReceiptTemplate(BaseProduction):
    __tablename__ = "standard_receipt_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    match_supplier_ico_norm: Mapped[str | None] = mapped_column(String(16), nullable=True)
    match_texts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_json: Mapped[str] = mapped_column(Text, nullable=False)
    sample_file_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    sample_file_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sample_file_relpath: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utc_now_naive)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utc_now_naive, onupdate=utc_now_naive)

    __table_args__ = (
        Index("idx_p_standard_receipt_templates_enabled", "enabled"),
        Index("idx_p_standard_receipt_templates_match_supplier_ico_norm", "match_supplier_ico_norm"),
        Index("idx_p_standard_receipt_templates_name", "name"),
    )


class DocumentPageAudit(BaseProduction):
    __tablename__ = "document_page_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)

    page_no: Mapped[int] = mapped_column(Integer)
    chosen_mode: Mapped[str] = mapped_column(String(16))
    chosen_score: Mapped[float] = mapped_column(Float, default=0.0)
    embedded_score: Mapped[float] = mapped_column(Float, default=0.0)
    ocr_score: Mapped[float] = mapped_column(Float, default=0.0)
    embedded_len: Mapped[int] = mapped_column(Integer, default=0)
    ocr_len: Mapped[int] = mapped_column(Integer, default=0)
    ocr_conf: Mapped[float] = mapped_column(Float, default=0.0)
    token_groups: Mapped[int] = mapped_column(Integer, default=0)

    document: Mapped[Document] = relationship(back_populates="page_audits")

    __table_args__ = (
        UniqueConstraint("document_id", "page_no", name="uq_p_page_audit_doc_page"),
        Index("ix_p_page_audit_doc_page", "document_id", "page_no"),
    )
