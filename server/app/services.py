"""予約まわりの業務ロジック。Web 画面・Stripe・Agent API から共通で使う。"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import clock
from .models import Booking, BookingStatus, Host, Slot, User
from .vpn import VpnProvider

log = logging.getLogger(__name__)

# 枠を埋めているとみなす状態 (支払い待ちは hold 時間内だけ)
TAKEN_STATUSES = (BookingStatus.CONFIRMED, BookingStatus.ACTIVE, BookingStatus.COMPLETED)
# 予定どおりの終わり方。これ以外で終了時刻より前に終わったら返金を検討する
NORMAL_END_REASONS = ("duration_elapsed", "server_cleanup", "server_cancelled")
# 借りた人が「いま終わる」を押した。早く終わっても、貸し手の都合ではない
BORROWER_ENDED = "borrower_ended"


class BookingError(Exception):
    pass


# --- 枠 -------------------------------------------------------------------


def blocking_booking(db: Session, slot: Slot, now: dt.datetime, hold_minutes: int) -> Booking | None:
    hold_from = now - dt.timedelta(minutes=hold_minutes)
    for b in slot.bookings:
        if b.status in TAKEN_STATUSES:
            return b
        if b.status == BookingStatus.PENDING_PAYMENT and b.created_at > hold_from:
            return b
    return None


# 始まっている枠に「今すぐ」入れるのは、遊べる時間がこれだけ残っていて、貸し手のアプリが動いているときだけ
NOW_MIN_PLAY = dt.timedelta(minutes=20)
# Agent は 15 秒ごとに問い合わせてくる。これより長く連絡がなければ PC は動いていないとみなす
ONLINE_WITHIN = dt.timedelta(minutes=3)


def host_online(host: Host, now: dt.datetime) -> bool:
    return host.last_seen_at is not None and now - host.last_seen_at <= ONLINE_WITHIN


def is_now_slot(slot: Slot, now: dt.datetime) -> bool:
    """もう始まっている枠 (「今すぐ」入る枠) か。"""
    return slot.start_at <= now


def is_available(db: Session, slot: Slot, now: dt.datetime, hold_minutes: int) -> bool:
    if slot.closed or not slot.host.active or blocking_booking(db, slot, now, hold_minutes) is not None:
        return False
    if slot.start_at > now:
        return True
    return slot.play_end_at - now >= NOW_MIN_PLAY and host_online(slot.host, now)


def available_slots(db: Session, now: dt.datetime, hold_minutes: int) -> list[Slot]:
    query = select(Slot).where(Slot.end_at > now).order_by(Slot.start_at)
    slots = db.scalars(query).all()
    stale = [s for s in slots if s.merged and blocking_booking(db, s, now, hold_minutes) is None]
    if stale:
        for s in stale:
            release_merged(db, s, now)  # 支払いが終わらないまま確保時間が切れた: 1 時間ずつに戻す
        db.commit()
        slots = db.scalars(query).all()
    return [s for s in slots if is_available(db, s, now, hold_minutes)]


def following_slots(db: Session, slot: Slot, now: dt.datetime, hold_minutes: int, max_hours: int = 0) -> list[Slot]:
    """slot のすぐ後に続いていて、まとめて予約できる空き枠 (同じ PC・すき間なし)。

    max_hours が 0 より大きければ、slot と合わせてその時間までにする。
    予約の履歴がある枠はつなげない (つなぐときに消すため)。
    """
    later = db.scalars(
        select(Slot).where(Slot.host_id == slot.host_id, Slot.start_at >= slot.end_at).order_by(Slot.start_at)
    ).all()
    chain: list[Slot] = []
    end = slot.end_at
    for s in later:
        if s.start_at != end or s.bookings or s.merged or not is_available(db, s, now, hold_minutes):
            break
        if max_hours and (s.end_at - slot.start_at) > dt.timedelta(hours=max_hours):
            break
        chain.append(s)
        end = s.end_at
    return chain


def merge_slots(db: Session, slot: Slot, chain: list[Slot]) -> None:
    """slot に、続く枠 chain をつないで 1 つの長い枠にする (料金は合計)。"""
    if not chain:
        return
    slot.end_at = chain[-1].end_at
    slot.price_yen += sum(s.price_yen for s in chain)
    slot.merged = True
    for s in chain:
        db.delete(s)
    db.flush()


def release_merged(db: Session, slot: Slot, now: dt.datetime) -> None:
    """つないだ枠を、まだ始まっていない部分だけ 1 時間ずつの枠に戻す (予約がキャンセルされたとき)。"""
    if not slot.merged:
        return
    hours = max(1, round((slot.end_at - slot.start_at) / SCHEDULE_CELL))
    per_hour = max(50, slot.price_yen // hours)
    cut = slot.start_at + SCHEDULE_CELL
    while cut <= now:
        cut += SCHEDULE_CELL
    end = slot.end_at
    if cut >= end:
        return
    slot.end_at = cut
    slot.price_yen = per_hour * max(1, round((cut - slot.start_at) / SCHEDULE_CELL))
    slot.merged = cut - slot.start_at > SCHEDULE_CELL
    t = cut
    while t < end:
        db.add(Slot(host=slot.host, start_at=t, end_at=min(t + SCHEDULE_CELL, end), price_yen=per_hour))
        t += SCHEDULE_CELL
    db.flush()


def create_slot(db: Session, host: Host, start_at: dt.datetime, end_at: dt.datetime, price_yen: int) -> Slot:
    if end_at <= start_at:
        raise BookingError("終了時刻は開始時刻より後にしてください")
    if price_yen < 50:
        raise BookingError("料金は 50 円以上にしてください (Stripe の最低額)")
    overlap = db.scalar(
        select(Slot).where(Slot.host_id == host.id, Slot.start_at < end_at, Slot.end_at > start_at)
    )
    if overlap:
        raise BookingError("同じ PC の別の枠と時間が重なっています")
    slot = Slot(host=host, start_at=start_at, end_at=end_at, price_yen=price_yen)
    db.add(slot)
    db.commit()
    return slot


# --- 予定表 (1 時間単位のカレンダー) ---------------------------------------

SCHEDULE_CELL = dt.timedelta(hours=1)
SCHEDULE_MAX_DAYS = 14


def iso_utc(value: dt.datetime) -> str:
    return value.replace(tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso_utc(value: str) -> dt.datetime:
    """ISO 8601 の日時を、タイムゾーンなしの UTC にする。タイムゾーンのないものは UTC とみなす。"""
    parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def until_text(seconds: float) -> str:
    """残り時間を「1時間23分」のような短い文章にする (Agent・予約ページの表示用)。"""
    minutes = int(seconds + 59) // 60  # 切り上げ: 「あと 0 分」と出さない
    if minutes >= 24 * 60:
        return f"{minutes // (24 * 60)}日{minutes % (24 * 60) // 60}時間"
    if minutes >= 60:
        return f"{minutes // 60}時間{minutes % 60}分"
    return f"{max(minutes, 1)}分"


def minutes_text(minutes: int) -> str:
    """利用時間を「55分」「1時間55分」「2日3時間」のように。"""
    minutes = max(0, int(minutes))
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = ([f"{days}日"] if days else []) + ([f"{hours}時間"] if hours else []) + ([f"{mins}分"] if mins or not minutes else [])
    return "".join(parts)


def slot_state(db: Session, slot: Slot, now: dt.datetime, hold_minutes: int) -> str:
    """open (受付中) / closed (受付をやめた) / holding (支払い待ち) / booked (予約あり) / past (終わった)。"""
    booking = blocking_booking(db, slot, now, hold_minutes)
    if booking is not None:
        return "booked" if booking.status in TAKEN_STATUSES else "holding"
    if slot.start_at <= now:
        return "past"
    return "closed" if slot.closed else "open"


def is_hour_cell(slot: Slot) -> bool:
    """カレンダーで 1 マスとして扱える枠 (ちょうど 1 時間・正時はじまり) か。"""
    return slot.end_at - slot.start_at == SCHEDULE_CELL and slot.start_at.minute == 0 and slot.start_at.second == 0


def _slots_between(db: Session, host: Host, start: dt.datetime, end: dt.datetime) -> list[Slot]:
    return list(db.scalars(
        select(Slot).where(Slot.host_id == host.id, Slot.start_at < end, Slot.end_at > start).order_by(Slot.start_at)
    ).all())


def host_schedule(db: Session, host: Host, start: dt.datetime, end: dt.datetime, now: dt.datetime,
                  hold_minutes: int) -> list[dict]:
    items = []
    for slot in _slots_between(db, host, start, end):
        state = slot_state(db, slot, now, hold_minutes)
        items.append({
            "id": slot.id,
            "start": iso_utc(slot.start_at),
            "end": iso_utc(slot.end_at),
            "price_yen": slot.price_yen,
            "state": state,
            # カレンダーで切り替えられるか (それ以外の枠は表示だけ)
            "editable": is_hour_cell(slot) and state in ("open", "closed"),
        })
    return items


def suggested_price(db: Session, host: Host) -> int:
    latest = db.scalar(select(Slot).where(Slot.host_id == host.id).order_by(Slot.id.desc()).limit(1))
    return latest.price_yen if latest else 500


def set_hourly_slots(db: Session, host: Host, start: dt.datetime, end: dt.datetime, starts: set[dt.datetime],
                     price_yen: int, now: dt.datetime, hold_minutes: int) -> dict:
    """[start, end) の受付時間を、starts (各 1 時間の開始時刻) のとおりにする。

    予約が入っている枠や支払い待ちの枠には触らない。外した枠は削除し、
    予約履歴があって削除できない枠は閉じる。
    """
    if price_yen < 50:
        raise BookingError("料金は 50 円以上にしてください")
    for s in starts:
        if not (start <= s < end) or s.minute or s.second or s.microsecond:
            raise BookingError("時間の指定が正しくありません")

    added = removed = 0
    errors: list[str] = []
    by_start: dict[dt.datetime, Slot] = {}
    for slot in _slots_between(db, host, start, end):
        if not is_hour_cell(slot):
            continue
        state = slot_state(db, slot, now, hold_minutes)
        if state not in ("open", "closed"):
            by_start[slot.start_at] = slot  # 予約あり・終わった枠: そのまま残す
            continue
        by_start[slot.start_at] = slot
        if slot.start_at in starts:
            if slot.closed:
                slot.closed = False
                added += 1
            slot.price_yen = price_yen
        elif state == "open":
            if slot.bookings:
                slot.closed = True
            else:
                db.delete(slot)
            removed += 1
    db.flush()

    for s in sorted(starts):
        if s in by_start:
            continue
        if s <= now:
            continue  # 過去の時間には作らない
        overlap = db.scalar(
            select(Slot).where(
                Slot.host_id == host.id, Slot.start_at < s + SCHEDULE_CELL, Slot.end_at > s, Slot.closed.is_(False)
            )
        )
        if overlap:
            errors.append(f"{iso_utc(s)} は別の枠と重なっています")
            continue
        db.add(Slot(host=host, start_at=s, end_at=s + SCHEDULE_CELL, price_yen=price_yen))
        added += 1
    db.commit()
    return {"added": added, "removed": removed, "errors": errors}


def next_booking_payload(booking: Booking | None, now: dt.datetime) -> dict | None:
    if booking is None:
        return None
    return {
        "booking_id": booking.id,
        "starts_at": iso_utc(booking.slot.start_at),
        "ends_at": iso_utc(booking.slot.play_end_at),
        "starts_in_seconds": max(0, int((booking.slot.start_at - now).total_seconds())),
    }


# --- 料金 -----------------------------------------------------------------


def trial_eligible(db: Session, user: User, now: dt.datetime, hold_minutes: int) -> bool:
    """初回無料の対象か。

    メールアドレスは使い捨てで量産できるため、Meta ユーザー名でも判定する
    (Virtual Desktop は登録した Meta ユーザー名でしか接続できないので、本人のものになる)。
    """
    hold_from = now - dt.timedelta(minutes=hold_minutes)
    conditions = [Booking.user_id == user.id]
    if user.meta_username:
        conditions.append(func.lower(User.meta_username) == user.meta_username.lower())
    used = db.scalars(
        select(Booking)
        .join(User, Booking.user_id == User.id)
        .where(Booking.discount_yen > 0)
        .where(conditions[0] if len(conditions) == 1 else (conditions[0] | conditions[1]))
    ).all()
    for b in used:
        if b.status in TAKEN_STATUSES:
            return False
        if b.status == BookingStatus.PENDING_PAYMENT and b.created_at > hold_from:
            return False
    return True


def trial_discount_yen(slot: Slot, trial_minutes: int) -> int:
    minutes = slot.duration_minutes
    if minutes <= 0 or trial_minutes <= 0:
        return 0
    return slot.price_yen * min(trial_minutes, minutes) // minutes


def price_now(slot: Slot, now: dt.datetime) -> int:
    """いま予約したときの料金。始まっている枠は、残りの利用時間の分だけにする。"""
    if slot.start_at >= now or slot.play_end_at <= slot.start_at:
        return slot.price_yen
    left = max(dt.timedelta(0), slot.play_end_at - now)
    return -(-slot.price_yen * int(left.total_seconds()) // int((slot.play_end_at - slot.start_at).total_seconds()))


def quote(db: Session, user: User, slot: Slot, now: dt.datetime, hold_minutes: int, trial_minutes: int,
          free: bool = False, chain: list[Slot] | None = None) -> tuple[int, int]:
    """(支払い額, 割引額) を返す。決済手段の最低額 (50円) を下回る端数は無料にする。free なら常に 0 円。

    chain は、続けて予約する枠 (following_slots の一部)。
    """
    if free:
        return 0, 0
    price = price_now(slot, now) + sum(s.price_yen for s in chain or [])
    discount = 0
    if trial_eligible(db, user, now, hold_minutes):
        discount = min(price, trial_discount_yen(slot, trial_minutes))
    amount = price - discount
    if 0 < amount < 50:
        discount, amount = price, 0
    return amount, discount


# --- 予約 -----------------------------------------------------------------


def chain_until(db: Session, slot: Slot, until_id: int, now: dt.datetime, hold_minutes: int,
                max_hours: int = 0) -> list[Slot]:
    """「何時まで」で選ばれた枠 until_id までの、続けて予約する枠。until_id が slot 自身や 0 なら空。"""
    if not until_id or until_id == slot.id:
        return []
    chain = following_slots(db, slot, now, hold_minutes, max_hours)
    ids = [s.id for s in chain]
    if until_id not in ids:
        raise BookingError("続けて予約しようとした時間が、もう埋まっています。もう一度えらんでください")
    return chain[:ids.index(until_id) + 1]


def create_booking(
    db: Session, user: User, slot: Slot, now: dt.datetime, hold_minutes: int, trial_minutes: int = 0,
    free: bool = False, chain: list[Slot] | None = None,
) -> Booking:
    if not user.meta_username:
        raise BookingError("先にアカウント設定で Meta (Quest) のユーザー名を登録してください")
    if not is_available(db, slot, now, hold_minutes):
        raise BookingError("この枠はすでに埋まっています")
    amount, discount = quote(db, user, slot, now, hold_minutes, trial_minutes, free, chain)
    merge_slots(db, slot, chain or [])
    booking = Booking(
        slot=slot, user=user, status=BookingStatus.PENDING_PAYMENT, created_at=now,
        amount_yen=amount, discount_yen=discount,
    )
    db.add(booking)
    db.commit()
    return booking


def taken_by_other(booking: Booking) -> Booking | None:
    return next((b for b in booking.slot.bookings if b.id != booking.id and b.status in TAKEN_STATUSES), None)


def confirm_booking(db: Session, booking: Booking, vpn: VpnProvider, now: dt.datetime) -> Booking:
    """支払い完了。冪等 (Stripe の Webhook は重複して届くことがある)。"""
    if booking.status != BookingStatus.PENDING_PAYMENT and booking.status != BookingStatus.EXPIRED:
        return booking

    if taken_by_other(booking):
        # 支払い待ちの確保時間が切れた後に別の人が先に確定させたケース
        booking.status = BookingStatus.CANCELLED
        booking.paid_at = now
        booking.note = "支払い完了前に枠が他の予約で埋まりました。返金が必要です。"
        log.error("二重予約: booking=%s slot=%s (返金が必要)", booking.id, booking.slot_id)
        db.commit()
        return booking

    booking.status = BookingStatus.CONFIRMED
    booking.paid_at = now
    host = booking.slot.host
    expiry = int((booking.slot.end_at - now).total_seconds())
    try:
        grant = vpn.issue(host.vpn_borrower_tag, expiry, f"booking-{booking.id}")
        if grant:
            booking.vpn_key_id = grant.key_id
            booking.vpn_auth_key = grant.auth_key
    except Exception as exc:  # noqa: BLE001 - 予約自体は確定させ、運営が手当てする
        log.exception("VPN キーの発行に失敗: booking=%s", booking.id)
        booking.note = f"VPN キーの発行に失敗しました: {exc}"
    db.commit()
    return booking


def expire_booking(db: Session, booking: Booking) -> None:
    if booking.status == BookingStatus.PENDING_PAYMENT:
        booking.status = BookingStatus.EXPIRED
        release_merged(db, booking.slot, clock.utcnow())
        db.commit()


def cancel_booking(db: Session, booking: Booking, vpn: VpnProvider, reason: str) -> None:
    if booking.status in (BookingStatus.COMPLETED, BookingStatus.CANCELLED):
        return
    was_paid = booking.paid_at is not None
    booking.status = BookingStatus.CANCELLED
    booking.note = (booking.note + "\n" if booking.note else "") + reason
    if was_paid:
        booking.note += "\n支払い済みのため、Stripe で返金してください。"
    _revoke_vpn(booking, vpn)
    release_merged(db, booking.slot, clock.utcnow())
    db.commit()


def _revoke_vpn(booking: Booking, vpn: VpnProvider) -> None:
    try:
        vpn.revoke(booking.vpn_key_id, booking.slot.host.vpn_borrower_tag)
        booking.vpn_auth_key = ""
    except Exception as exc:  # noqa: BLE001
        log.exception("VPN の取り消しに失敗: booking=%s", booking.id)
        booking.note = (booking.note + "\n" if booking.note else "") + f"VPN の取り消しに失敗: {exc}"


# --- Agent ----------------------------------------------------------------


def _start_payload(booking: Booking, now: dt.datetime, resume: bool) -> dict:
    return {
        "action": "start",
        "booking_id": booking.id,
        "remaining_seconds": max(0, int((booking.slot.play_end_at - now).total_seconds())),
        "starts_in_seconds": max(0, int((booking.slot.start_at - now).total_seconds())),
        "borrower_meta_username": booking.user.meta_username,
        "resume": resume,
    }


def agent_poll(db: Session, host: Host, now: dt.datetime, lead_seconds: int, vpn: VpnProvider) -> dict:
    """Agent からの「次の仕事はある？」に答える。"""
    host.last_seen_at = now
    host_bookings = db.scalars(
        select(Booking)
        .join(Slot)
        .where(
            Slot.host_id == host.id,
            Booking.status.in_((BookingStatus.CONFIRMED, BookingStatus.ACTIVE)),
        )
        .order_by(Slot.start_at)
    ).all()

    try:
        # Agent が再起動した: 利用中のまま残っている予約を再開するか片付ける
        for b in host_bookings:
            if b.status == BookingStatus.ACTIVE:
                if b.slot.play_end_at <= now:
                    return {"action": "cleanup", "booking_id": b.id}
                return _start_payload(b, now, resume=True)

        for b in host_bookings:
            if b.slot.play_end_at <= now:
                # PC がオフラインなどで開始できないまま終わった
                b.status = BookingStatus.EXPIRED
                b.note = (b.note + "\n" if b.note else "") + "PC が応答せず開始できませんでした。返金が必要です。"
                _revoke_vpn(b, vpn)
                log.error("開始されなかった予約: booking=%s", b.id)
                continue
            if b.slot.start_at - dt.timedelta(seconds=lead_seconds) <= now:
                return _start_payload(b, now, resume=False)
        # 次の予約を知らせ、Agent の画面に「あと何分で開始」を出す
        upcoming = next((b for b in host_bookings if b.status == BookingStatus.CONFIRMED and b.slot.play_end_at > now), None)
        return {"action": "none", "next": next_booking_payload(upcoming, now)}
    finally:
        db.commit()


# --- スクリーンショット ---------------------------------------------------


def screenshots_available(booking: Booking, media_root: Path, now: dt.datetime) -> bool:
    return bool(
        booking.screenshots_file
        and booking.screenshots_expires_at
        and booking.screenshots_expires_at > now
        and (media_root / booking.screenshots_file).is_file()
    )


def purge_expired_screenshots(db: Session, media_root: Path, now: dt.datetime) -> int:
    """保存期間を過ぎたスクリーンショットを削除する。"""
    expired = db.scalars(
        select(Booking).where(Booking.screenshots_file != "", Booking.screenshots_expires_at <= now)
    ).all()
    for b in expired:
        try:
            (media_root / b.screenshots_file).unlink(missing_ok=True)
        except OSError:
            log.exception("スクリーンショットを削除できません: booking=%s", b.id)
            continue
        b.screenshots_file = ""
    if expired:
        db.commit()
        log.info("期限切れのスクリーンショットを %d 件削除しました", len(expired))
    return len(expired)


def mark_started(db: Session, booking: Booking, now: dt.datetime) -> None:
    if booking.status == BookingStatus.CONFIRMED:
        booking.status = BookingStatus.ACTIVE
        booking.started_at = now
        db.commit()


def end_now(db: Session, booking: Booking, vpn: VpnProvider, now: dt.datetime) -> bool:
    """借りた人が、時間の途中で「いま終わる」を押した。始まっていなければ何もしない (False)。

    予約を「終了」にすると、PC のアプリは 30 秒ほどでそれに気づいて、VRChat を閉じて
    ログイン情報を消す (いつもの終了処理)。終わったあとの報告は mark_finished で受け取る。
    """
    started = booking.status == BookingStatus.ACTIVE or (
        booking.status == BookingStatus.CONFIRMED and booking.slot.start_at <= now)
    if not started or booking.slot.play_end_at <= now:
        return False
    left = int((booking.slot.play_end_at - now).total_seconds() // 60)
    booking.status = BookingStatus.COMPLETED
    booking.end_reason = BORROWER_ENDED
    booking.note = (booking.note + "\n" if booking.note else "") + f"借りた人が「いま終わる」を押しました（残り {left} 分）。"
    _revoke_vpn(booking, vpn)
    db.commit()
    return True


def mark_finished(db: Session, booking: Booking, report: dict, vpn: VpnProvider, now: dt.datetime) -> None:
    ended_by_borrower = booking.end_reason == BORROWER_ENDED
    if booking.status in (BookingStatus.CONFIRMED, BookingStatus.ACTIVE):
        booking.status = BookingStatus.COMPLETED
    booking.finished_at = now
    booking.end_reason = BORROWER_ENDED if ended_by_borrower else str(report.get("end_reason", ""))[:64]
    booking.clean = bool(report.get("clean"))
    booking.report_json = json.dumps(report, ensure_ascii=False)[:20000]
    if not booking.clean:
        log.error("クリーンアップにエラーあり: booking=%s", booking.id)
    lost_minutes = int((booking.slot.play_end_at - now).total_seconds() // 60)
    if booking.end_reason not in (*NORMAL_END_REASONS, BORROWER_ENDED) and lost_minutes >= 1:
        # 貸し手 PC 側の都合 (Ctrl+C・起動失敗・想定外のエラー) で早く終わった
        booking.note = (booking.note + "\n" if booking.note else "") + (
            f"終了予定の {lost_minutes} 分前に終了しました（{booking.end_reason}）。返金を検討してください。"
        )
    _revoke_vpn(booking, vpn)
    db.commit()
