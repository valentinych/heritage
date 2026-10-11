from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Table, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

LINK_PROPOSED = "proposed"
LINK_CONFIRMED = "confirmed"
LINK_REJECTED = "rejected"
LINK_STATUSES = {LINK_PROPOSED, LINK_CONFIRMED, LINK_REJECTED}

KIND_MATCHES = "matches"
KIND_CLUSTERS = "clusters"
KIND_LINKS = "links"


class Base(DeclarativeBase):
    pass


family_child = Table(
    "family_children",
    Base.metadata,
    Column("family_id", ForeignKey("families.id", ondelete="CASCADE"), primary_key=True),
    Column("person_id", ForeignKey("people.id", ondelete="CASCADE"), primary_key=True),
    Column("position", Integer, nullable=False, default=0),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    must_set_password: Mapped[bool] = mapped_column(default=False)
    is_admin: Mapped[bool] = mapped_column(default=False)
    email: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Space(Base):
    __tablename__ = "spaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    cluster_self: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Membership(Base):
    __tablename__ = "memberships"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), primary_key=True)


class Tree(Base):
    __tablename__ = "trees"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    person_count: Mapped[int] = mapped_column(default=0)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Person(Base):
    __tablename__ = "people"
    __table_args__ = (UniqueConstraint("tree_id", "gedcom_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tree_id: Mapped[int] = mapped_column(ForeignKey("trees.id", ondelete="CASCADE"), index=True)
    gedcom_id: Mapped[str] = mapped_column(String(64))
    given_name: Mapped[str] = mapped_column(String(255), default="")
    surname: Mapped[str] = mapped_column(String(255), default="")
    full_name: Mapped[str] = mapped_column(String(500))
    name_key: Mapped[str] = mapped_column(String(500), index=True, default="")
    sex: Mapped[str] = mapped_column(String(1), default="")
    birth_date: Mapped[str] = mapped_column(String(80), default="")
    birth_year: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    death_date: Mapped[str] = mapped_column(String(80), default="")
    death_year: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Family(Base):
    __tablename__ = "families"
    __table_args__ = (UniqueConstraint("tree_id", "gedcom_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tree_id: Mapped[int] = mapped_column(ForeignKey("trees.id", ondelete="CASCADE"), index=True)
    gedcom_id: Mapped[str] = mapped_column(String(64))
    husband_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"), nullable=True)
    wife_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"), nullable=True)


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), default="mapping")
    column_map: Mapped[str | None] = mapped_column(Text, nullable=True)
    row_count: Mapped[int] = mapped_column(default=0)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    owner_name: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DnaMatch(Base):
    __tablename__ = "dna_matches"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(500))
    shared_cm: Mapped[float | None] = mapped_column(nullable=True)
    largest_segment: Mapped[float | None] = mapped_column(nullable=True)


class ClusterMember(Base):
    __tablename__ = "cluster_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    cluster_label: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(500))
    shared_cm: Mapped[float | None] = mapped_column(nullable=True)
    kit: Mapped[str] = mapped_column(String(64), default="")
    segments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    largest_cm: Mapped[float | None] = mapped_column(nullable=True)
    icw: Mapped[int | None] = mapped_column(Integer, nullable=True)
    relationship: Mapped[str] = mapped_column(String(120), default="")
    tree_url: Mapped[str] = mapped_column(String(1000), default="")
    tree_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(String(500), default="")


class ClusterPair(Base):
    __tablename__ = "cluster_pairs"
    __table_args__ = (UniqueConstraint("member_low_id", "member_high_id", name="uq_cluster_pair"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    member_low_id: Mapped[int] = mapped_column(ForeignKey("cluster_members.id", ondelete="CASCADE"), index=True)
    member_high_id: Mapped[int] = mapped_column(ForeignKey("cluster_members.id", ondelete="CASCADE"), index=True)


class LinkBuild(Base):
    __tablename__ = "link_builds"

    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), primary_key=True)
    people: Mapped[str] = mapped_column(Text, default="[]")


class LinkRow(Base):
    __tablename__ = "link_rows"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(500))
    name_key: Mapped[str] = mapped_column(String(500), default="")
    birth_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), default="pending")
    match_count: Mapped[int] = mapped_column(default=0)
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), nullable=True, default=None, index=True
    )


class GedMatchKit(Base):
    __tablename__ = "gedmatch_kits"
    __table_args__ = (UniqueConstraint("space_id", "source_kit", name="uq_gedmatch_owner"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    source_kit: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(500), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    company: Mapped[str] = mapped_column(String(80), default="")
    sex: Mapped[str] = mapped_column(String(1), default="")


class GedMatchHit(Base):
    __tablename__ = "gedmatch_hits"
    __table_args__ = (UniqueConstraint("space_id", "source_kit", "match_kit", name="uq_gedmatch_kit"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    source_kit: Mapped[str] = mapped_column(String(32))
    match_kit: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(500), default="")
    name_key: Mapped[str] = mapped_column(String(500), default="")
    total_cm: Mapped[float | None] = mapped_column(nullable=True)
    largest_cm: Mapped[float | None] = mapped_column(nullable=True)
    overlap: Mapped[float | None] = mapped_column(nullable=True)
    generations: Mapped[float | None] = mapped_column(nullable=True)
    date_compared: Mapped[str] = mapped_column(String(32), default="")
    company: Mapped[str] = mapped_column(String(80), default="")
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), nullable=True, default=None, index=True
    )


class GedMatchMatrix(Base):
    __tablename__ = "gedmatch_matrices"
    __table_args__ = (UniqueConstraint("space_id", "name", name="uq_gedmatch_matrix_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    receives_unlisted: Mapped[bool] = mapped_column(default=False)


class GedMatchMatrixKit(Base):
    __tablename__ = "gedmatch_matrix_kits"

    matrix_id: Mapped[int] = mapped_column(
        ForeignKey("gedmatch_matrices.id", ondelete="CASCADE"), primary_key=True
    )
    kit: Mapped[str] = mapped_column(String(32), primary_key=True)


class GedMatchKinship(Base):
    __tablename__ = "gedmatch_kinships"
    __table_args__ = (UniqueConstraint("space_id", "kit_low", "kit_high", name="uq_gedmatch_kinship"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    kit_low: Mapped[str] = mapped_column(String(32))
    kit_high: Mapped[str] = mapped_column(String(32))
    relation: Mapped[str] = mapped_column(String(16))
    parent_kit: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ArtifactSettlement(Base):
    """An artifact the grouping could not place, attached to a settlement by hand."""

    __tablename__ = "artifact_settlements"

    artifact_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    settlement_id: Mapped[str] = mapped_column(String(255))


class PersonLink(Base):
    __tablename__ = "person_links"
    __table_args__ = (UniqueConstraint("person_low_id", "person_high_id", name="uq_person_link_pair"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    dataset_id: Mapped[int | None] = mapped_column(ForeignKey("datasets.id", ondelete="SET NULL"), nullable=True)
    person_low_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    person_high_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default=LINK_PROPOSED)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
