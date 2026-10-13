"""お得意さん関係: フォロー・評価。"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Booking, BookingStatus, Follow, Host, Review, Slot, User

REVIEW_DAYS = 30
NICKNAME_MAX = 32
COMMENT_MAX = 300


class SocialError(Exception):
    pass


# --- フォロー -------------------------------------------------------------------


def relation(db: Session, user: User, lender: User) -> Follow | None:
    return db.scalar(select(Follow).where(Follow.user_id == user.id, Follow.lender_id == lender.id))


def set_following(db: Session, user: User, lender: User, on: bool) -> None:
    if user.id == lender.id:
        raise SocialError("自分はフォローできません")
    rel = relation(db, user, lender)
    if rel is None:
        rel = Follow(user_id=user.id, lender_id=lender.id)
        db.add(rel)
    rel.following = on
    db.commit()


def followers(db: Session, lender: User) -> list[User]:
    return list(db.scalars(
        select(User).join(Follow, Follow.user_id == User.id)
        .where(Follow.lender_id == lender.id, Follow.following.is_(True)).order_by(Follow.created_at)
    ).all())


def following_lenders(db: Session, user: User) -> list[User]:
    return list(db.scalars(
        select(User).join(Follow, Follow.lender_id == User.id)
        .where(Follow.user_id == user.id, Follow.following.is_(True)).order_by(Follow.created_at)
    ).all())


def follower_count(db: Session, lender: User) -> int:
    return db.scalar(
        select(func.count()).select_from(Follow).where(Follow.lender_id == lender.id, Follow.following.is_(True))
    ) or 0


# --- 評価 ---------------------------------------------------------------------


def rating(db: Session, target: User, author_role: str) -> tuple[float | None, int]:
    """(平均の星, 件数)。author_role="borrower" なら貸し手としての評価。"""
    avg, count = db.execute(
        select(func.avg(Review.stars), func.count(Review.id))
        .where(Review.target_id == target.id, Review.role == author_role)
    ).one()
    return (round(float(avg), 1) if avg is not None else None, int(count or 0))


def lender_rating(db: Session, lender: User) -> tuple[float | None, int]:
    return rating(db, lender, "borrower")


def borrower_rating(db: Session, borrower: User) -> tuple[float | None, int]:
    return rating(db, borrower, "lender")


def reviews_of_lender(db: Session, lender: User, limit: int = 20) -> list[Review]:
    return list(db.scalars(
        select(Review).where(Review.target_id == lender.id, Review.role == "borrower")
        .order_by(Review.created_at.desc()).limit(limit)
    ).all())


def review_role(booking: Booking, user: User) -> str | None:
    """この人がこの予約を評価できる立場か。"""
    if booking.user_id == user.id:
        return "borrower"
    if booking.slot.host.owner_id == user.id:
        return "lender"
    return None


def can_review(db: Session, booking: Booking, user: User, now: dt.datetime) -> bool:
    role = review_role(booking, user)
    if role is None or booking.status != BookingStatus.COMPLETED:
        return False
    if role == "borrower" and booking.slot.host.owner_id is None:
        return False  # 運営が登録した PC には評価する相手がいない
    if booking.finished_at and now - booking.finished_at > dt.timedelta(days=REVIEW_DAYS):
        return False
    return my_review(db, booking, user) is None


def my_review(db: Session, booking: Booking, user: User) -> Review | None:
    return db.scalar(select(Review).where(Review.booking_id == booking.id, Review.author_id == user.id))


def add_review(db: Session, booking: Booking, user: User, stars: int, comment: str, now: dt.datetime) -> Review:
    if not can_review(db, booking, user, now):
        raise SocialError("この予約は評価できません")
    if not 1 <= stars <= 5:
        raise SocialError("星は 1〜5 で選んでください")
    role = review_role(booking, user)
    target_id = booking.slot.host.owner_id if role == "borrower" else booking.user_id
    review = Review(
        booking_id=booking.id, author_id=user.id, target_id=target_id, role=role,
        stars=stars, comment=comment.strip()[:COMMENT_MAX], created_at=now,
    )
    db.add(review)
    db.commit()
    return review


# --- 貸し手ページ用 -------------------------------------------------------------


def lender_hosts(db: Session, lender: User) -> list[Host]:
    return list(db.scalars(select(Host).where(Host.owner_id == lender.id).order_by(Host.id)).all())


def lender_bookings(db: Session, lender: User, statuses: tuple[str, ...], limit: int = 30) -> list[Booking]:
    return list(db.scalars(
        select(Booking).join(Slot).join(Host)
        .where(Host.owner_id == lender.id, Booking.status.in_(statuses))
        .order_by(Slot.start_at.desc()).limit(limit)
    ).all())


def followers_to_notify(db: Session, host: Host) -> list[User]:
    """新しい時間を公開したときに知らせる人。"""
    return followers(db, host.owner) if host.owner else []
