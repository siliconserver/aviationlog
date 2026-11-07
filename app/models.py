"""Database models.

Master data (aircraft, airports, airlines, types) is imported from public sources.
Every master data row has a ``manual_fields`` list: fields edited by hand are listed
there and are never overwritten by a later import.
"""
import enum
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class Role(str, enum.Enum):
    admin = "admin"
    viewer = "viewer"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"), default=Role.viewer)
    language: Mapped[str | None] = mapped_column(String(8))
    theme: Mapped[str] = mapped_column(String(8), default="system")
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    recovery_codes: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Airline(Base):
    __tablename__ = "airlines"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    alias: Mapped[str | None] = mapped_column(String(255))
    iata: Mapped[str | None] = mapped_column(String(4), index=True)
    icao: Mapped[str | None] = mapped_column(String(4), index=True)
    callsign: Mapped[str | None] = mapped_column(String(64))
    country: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    manual_fields: Mapped[list] = mapped_column(JSONB, default=list)


class AircraftType(Base):
    __tablename__ = "aircraft_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    icao: Mapped[str] = mapped_column(String(8), unique=True)
    iata: Mapped[str | None] = mapped_column(String(8))
    name: Mapped[str | None] = mapped_column(String(255))
    manufacturer: Mapped[str | None] = mapped_column(String(255))
    model_full: Mapped[str | None] = mapped_column(String(255))
    engine_count: Mapped[str | None] = mapped_column(String(8))
    engine_type: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(String(32))
    manual_fields: Mapped[list] = mapped_column(JSONB, default=list)

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        if self.manufacturer and self.model_full:
            return f"{self.manufacturer.title()} {self.model_full}"
        return self.icao


class Airport(Base):
    __tablename__ = "airports"

    id: Mapped[int] = mapped_column(primary_key=True)
    ident: Mapped[str] = mapped_column(String(16), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    iata: Mapped[str | None] = mapped_column(String(4), index=True)
    icao: Mapped[str | None] = mapped_column(String(4), index=True)
    type: Mapped[str | None] = mapped_column(String(32))
    municipality: Mapped[str | None] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(8))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    manual_fields: Mapped[list] = mapped_column(JSONB, default=list)

    spots: Mapped[list["Spot"]] = relationship(back_populates="airport", order_by="Spot.name")

    @property
    def code(self) -> str:
        return self.iata or self.icao or self.ident


class Spot(Base):
    """A named spotting location at an airport (e.g. "Visitors hill MUC")."""

    __tablename__ = "spots"

    id: Mapped[int] = mapped_column(primary_key=True)
    airport_id: Mapped[int] = mapped_column(ForeignKey("airports.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)

    airport: Mapped[Airport] = relationship(back_populates="spots")


class Aircraft(Base):
    __tablename__ = "aircraft"

    id: Mapped[int] = mapped_column(primary_key=True)
    registration: Mapped[str] = mapped_column(String(16), unique=True)
    icao24: Mapped[str | None] = mapped_column(String(8))
    msn: Mapped[str | None] = mapped_column(String(32), index=True)
    line_number: Mapped[str | None] = mapped_column(String(32))
    type_icao: Mapped[str | None] = mapped_column(String(8), index=True)
    model: Mapped[str | None] = mapped_column(String(255))
    manufacturer: Mapped[str | None] = mapped_column(String(255))
    operator_name: Mapped[str | None] = mapped_column(String(255))
    operator_icao: Mapped[str | None] = mapped_column(String(4), index=True)
    operator_iata: Mapped[str | None] = mapped_column(String(4))
    airline_id: Mapped[int | None] = mapped_column(ForeignKey("airlines.id", ondelete="SET NULL"), index=True)
    christening_name: Mapped[str | None] = mapped_column(String(255))
    built: Mapped[str | None] = mapped_column(String(10))
    first_flight: Mapped[str | None] = mapped_column(String(10))
    delivery_date: Mapped[date | None] = mapped_column(Date)
    production_site: Mapped[str | None] = mapped_column(String(255))
    engines: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # registration not known yet: placeholder registration "?-<n>", not counted in fleet completion
    reg_unknown: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    notes: Mapped[str | None] = mapped_column(Text)
    cover_photo_id: Mapped[int | None] = mapped_column(
        ForeignKey("photos.id", ondelete="SET NULL", use_alter=True, name="fk_aircraft_cover_photo")
    )
    source: Mapped[str] = mapped_column(String(16), default="manual")
    manual_fields: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    airline: Mapped[Airline | None] = relationship()
    aircraft_type: Mapped[AircraftType | None] = relationship(
        primaryjoin="foreign(Aircraft.type_icao) == AircraftType.icao", viewonly=True
    )
    sightings: Mapped[list["Sighting"]] = relationship(
        back_populates="aircraft", order_by="Sighting.date.desc()", cascade="all, delete-orphan"
    )
    cover_photo: Mapped["Photo | None"] = relationship(foreign_keys=[cover_photo_id], post_update=True)

    @property
    def age_years(self) -> float | None:
        start = None
        if self.delivery_date:
            start = self.delivery_date
        elif self.built and self.built[:4].isdigit():
            try:
                start = date.fromisoformat(self.built) if len(self.built) == 10 else date(int(self.built[:4]), 7, 1)
            except ValueError:
                start = date(int(self.built[:4]), 7, 1)
        if not start:
            return None
        return round((date.today() - start).days / 365.25, 1)


class Sighting(Base):
    __tablename__ = "sightings"

    id: Mapped[int] = mapped_column(primary_key=True)
    aircraft_id: Mapped[int] = mapped_column(ForeignKey("aircraft.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    airport_id: Mapped[int | None] = mapped_column(ForeignKey("airports.id", ondelete="SET NULL"), index=True)
    spot_id: Mapped[int | None] = mapped_column(ForeignKey("spots.id", ondelete="SET NULL"), index=True)
    livery: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    folder: Mapped[str | None] = mapped_column(String(1024), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    aircraft: Mapped[Aircraft] = relationship(back_populates="sightings")
    airport: Mapped[Airport | None] = relationship()
    spot: Mapped[Spot | None] = relationship()
    photos: Mapped[list["Photo"]] = relationship(
        back_populates="sighting", order_by="Photo.path", cascade="all, delete-orphan"
    )

    @property
    def gallery(self) -> list["Photo"]:
        """Photos to show: a RAW file is hidden when a JPEG/HEIF with the same name exists."""
        stems = {p.stem for p in self.photos if not p.is_raw}
        return [p for p in self.photos if not (p.is_raw and p.stem in stems)]


class Photo(Base):
    __tablename__ = "photos"

    id: Mapped[int] = mapped_column(primary_key=True)
    sighting_id: Mapped[int] = mapped_column(ForeignKey("sightings.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(1024), unique=True)
    size: Mapped[int | None] = mapped_column(BigInteger)
    mtime: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    camera_make: Mapped[str | None] = mapped_column(String(128), index=True)
    camera_model: Mapped[str | None] = mapped_column(String(128), index=True)
    lens: Mapped[str | None] = mapped_column(String(255), index=True)
    focal_length: Mapped[float | None] = mapped_column(Float)
    aperture: Mapped[float | None] = mapped_column(Float)
    exposure: Mapped[str | None] = mapped_column(String(32))
    iso: Mapped[int | None] = mapped_column(Integer)
    taken_at: Mapped[datetime | None] = mapped_column(DateTime)
    gps_lat: Mapped[float | None] = mapped_column(Float)
    gps_lon: Mapped[float | None] = mapped_column(Float)
    exif_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sighting: Mapped[Sighting] = relationship(back_populates="photos")

    @property
    def filename(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def stem(self) -> str:
        return self.path.rsplit(".", 1)[0]

    @property
    def is_raw(self) -> bool:
        return self.path.lower().endswith(".arw")

    @property
    def raw_companion(self) -> "Photo | None":
        if self.is_raw:
            return None
        for other in self.sighting.photos:
            if other.is_raw and other.stem == self.stem:
                return other
        return None

    @property
    def camera(self) -> str | None:
        if not self.camera_model:
            return None
        make = (self.camera_make or "").strip()
        model = self.camera_model.strip()
        if make and not model.lower().startswith(make.split()[0].lower()):
            return f"{make} {model}"
        return model


class Job(Base):
    """Background job (imports, folder scans, thumbnails) with progress."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued, running, done, failed
    progress: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    # loading, ready, applying, applied, discarded, failed
    status: Mapped[str] = mapped_column(String(16), default="loading")
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImportRow(Base):
    """One staged record of an import. After the diff only new and changed rows remain."""

    __tablename__ = "import_rows"
    __table_args__ = (
        UniqueConstraint("batch_id", "entity", "key"),
        Index("ix_import_rows_batch_kind", "batch_id", "kind", "in_collection"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"))
    entity: Mapped[str] = mapped_column(String(16))
    key: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSONB)
    old: Mapped[dict | None] = mapped_column(JSONB)
    kind: Mapped[str | None] = mapped_column(String(8))  # new, changed
    in_collection: Mapped[bool] = mapped_column(Boolean, default=False)
    accepted: Mapped[bool] = mapped_column(Boolean, default=True)
    edited: Mapped[dict] = mapped_column(JSONB, default=dict)


class ScanEntry(Base):
    """A date folder found by the photo archive scan, waiting for review."""

    __tablename__ = "scan_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    folder: Mapped[str] = mapped_column(String(1024))
    parsed: Mapped[dict] = mapped_column(JSONB, default=dict)
    problems: Mapped[list] = mapped_column(JSONB, default=list)
    files: Mapped[list] = mapped_column(JSONB, default=list)
    sighting_id: Mapped[int | None] = mapped_column(Integer)
    accepted: Mapped[bool] = mapped_column(Boolean, default=True)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)


class FolderOverride(Base):
    """Corrections made in the scan review, remembered per folder."""

    __tablename__ = "folder_overrides"

    folder: Mapped[str] = mapped_column(String(1024), primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
