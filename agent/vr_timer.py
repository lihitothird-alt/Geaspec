"""VR の中に残り時間を出す (SteamVR のオーバーレイ)。

- 左手のコントローラーの手首のあたりに、腕時計のような「残り 42分」を出す
  (左手が見つからないときは、視界の左下に小さく出す)
- 残り 10分・5分・1分になったら、目の前に数秒だけお知らせを出す

VRChat の中ではなく SteamVR の上に重ねて表示するので、ほかの人には見えない。
SteamVR が動いていないときは、動き出すまで待つ。表示できなくても利用そのものには影響しない。
"""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

NOTICE_AT = (10 * 60, 5 * 60, 60)
NOTICE_SECONDS = 6.0
WATCH_SIZE = (320, 128)
NOTICE_SIZE = (1024, 200)
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\YuGothB.ttc",
    r"C:\Windows\Fonts\meiryob.ttc",
    r"C:\Windows\Fonts\meiryo.ttc",
    r"C:\Windows\Fonts\msgothic.ttc",
)


def timer_text(remaining: float) -> str:
    remaining = max(0.0, remaining)
    if remaining >= 60:
        return f"残り {math.ceil(remaining / 60)}分"
    return f"残り {int(remaining)}秒"


def notice_text(mark: int) -> str:
    return f"あと {mark // 60}分で終了します"


def due_notice(remaining: float, shown: set[int]) -> int | None:
    """いま出すお知らせ (10分・5分・1分) を返す。はじめから過ぎていたものは出さない。"""
    due = [m for m in NOTICE_AT if remaining <= m and m not in shown]
    if not due:
        return None
    shown.update(due)
    mark = min(due)
    # 例: 残り 3 分で始まった利用では、10 分と 5 分のお知らせは出さない
    return mark if remaining > mark - 30 else None


def steamvr_running() -> bool:
    try:
        import psutil
    except ImportError:
        return True
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info.get("name") or "").lower() == "vrserver.exe":
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def _font(size: int):
    from PIL import ImageFont

    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # 古い Pillow
        return ImageFont.load_default()


def render(text: str, size: tuple[int, int], path: Path, accent: bool = False) -> Path:
    """角の丸い黒い板に白い文字の画像を作る。"""
    from PIL import Image, ImageDraw

    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    fill = (91, 60, 240, 235) if accent else (17, 14, 33, 215)
    draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 4, fill=fill, outline=(169, 150, 255, 255), width=4)
    # 板の幅の 8 割に収まる大きさにする
    size_px = int(h * 0.48)
    while True:
        font = _font(size_px)
        box = draw.textbbox((0, 0), text, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
        if tw <= w * 0.8 or size_px <= 12:
            break
        size_px -= 2
    draw.text(((w - tw) / 2 - box[0], (h - th) / 2 - box[1]), text, font=font, fill=(255, 255, 255, 255))
    img.save(path)
    return path


def _matrix(openvr, tx: float, ty: float, tz: float, tilt_deg: float = 0.0):
    """X 軸まわりに tilt_deg 傾けて (tx, ty, tz) に置く変換。"""
    m = openvr.HmdMatrix34_t()
    a = math.radians(tilt_deg)
    rows = ((1, 0, 0, tx), (0, math.cos(a), -math.sin(a), ty), (0, math.sin(a), math.cos(a), tz))
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            m.m[r][c] = v
    return m


class VrTimer:
    """セッションの間、別のスレッドで VR の中に残り時間を出し続ける。"""

    def __init__(self, deadline: float, folder: Path, clock=time.monotonic) -> None:
        self.deadline = deadline  # time.monotonic() の値
        self.folder = folder
        self.clock = clock
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.shown: set[int] = set()
        self._flip = 0

    def remaining(self) -> float:
        return self.deadline - self.clock()

    def start(self) -> "VrTimer":
        self.folder.mkdir(parents=True, exist_ok=True)
        self.thread = threading.Thread(target=self._run, name="geaspec-vr-timer", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _image(self, name: str, text: str, size: tuple[int, int], accent: bool = False) -> str:
        # 同じファイル名だと SteamVR が読み直さないことがあるので、2 つの名前を交互に使う
        self._flip ^= 1
        return str(render(text, size, self.folder / f"{name}-{self._flip}.png", accent).resolve())

    def _run(self) -> None:
        try:
            import openvr  # noqa: F401
        except Exception as exc:  # noqa: BLE001 - 表示できなくても利用は続ける
            log.warning("VR の中のタイマーは使えません: %s", exc)
            return
        while not self.stop_event.is_set() and self.remaining() > 0:
            # SteamVR が動いていないときに接続すると、SteamVR を起動してしまう。動き出すまで待つ
            if not steamvr_running():
                self.stop_event.wait(5)
                continue
            try:
                self._show_until_stopped()
            except Exception as exc:  # noqa: BLE001 - SteamVR がまだ動いていない・再起動した
                log.debug("VR タイマー: SteamVR を待っています (%s)", exc)
                self._shutdown()
                self.stop_event.wait(10)

    def _show_until_stopped(self) -> None:
        import openvr

        system = openvr.init(openvr.VRApplication_Overlay)
        self._connected = True
        overlay = openvr.VROverlay()
        watch = overlay.createOverlay("geaspec.timer", "Geaspec 残り時間")
        notice = overlay.createOverlay("geaspec.notice", "Geaspec お知らせ")
        log.info("VR の中に残り時間を表示します")
        for handle in (watch, notice):
            overlay.setOverlaySortOrder(handle, 100)
        overlay.setOverlayWidthInMeters(notice, 0.5)
        overlay.setOverlayTransformTrackedDeviceRelative(
            notice, openvr.k_unTrackedDeviceIndex_Hmd, _matrix(openvr, 0.0, -0.05, -0.8)
        )
        attached_to = None
        last_text = ""
        notice_until = 0.0
        last_check = self.clock()
        while not self.stop_event.is_set():
            remaining = self.remaining()
            if remaining <= 0:
                break
            if self.clock() - last_check >= 5:
                last_check = self.clock()
                if not steamvr_running():
                    break  # 借り手が SteamVR を閉じた。動き出したらまた表示する
            # 左手が見つかれば手首に、見つからなければ視界の左下に
            hand = system.getTrackedDeviceIndexForControllerRole(openvr.TrackedControllerRole_LeftHand)
            target = hand if hand != openvr.k_unTrackedDeviceIndexInvalid else openvr.k_unTrackedDeviceIndex_Hmd
            if target != attached_to:
                if target == openvr.k_unTrackedDeviceIndex_Hmd:
                    overlay.setOverlayWidthInMeters(watch, 0.09)
                    m = _matrix(openvr, -0.16, -0.13, -0.6)
                else:
                    overlay.setOverlayWidthInMeters(watch, 0.08)
                    m = _matrix(openvr, 0.0, 0.03, 0.12, tilt_deg=-60)
                overlay.setOverlayTransformTrackedDeviceRelative(watch, target, m)
                attached_to = target
            text = timer_text(remaining)
            if text != last_text:
                overlay.setOverlayFromFile(watch, self._image("watch", text, WATCH_SIZE, accent=remaining < 60))
                overlay.showOverlay(watch)
                last_text = text
            mark = due_notice(remaining, self.shown)
            if mark:
                overlay.setOverlayFromFile(notice, self._image("notice", notice_text(mark), NOTICE_SIZE, accent=True))
                overlay.showOverlay(notice)
                notice_until = self.clock() + NOTICE_SECONDS
            elif notice_until and self.clock() >= notice_until:
                overlay.hideOverlay(notice)
                notice_until = 0.0
            self.stop_event.wait(1.0)
        for handle in (watch, notice):
            overlay.destroyOverlay(handle)
        self._shutdown()

    def _shutdown(self) -> None:
        if getattr(self, "_connected", False):
            self._connected = False
            try:
                import openvr

                openvr.shutdown()
            except Exception:  # noqa: BLE001
                pass


def start_vr_timer(deadline: float, folder: Path) -> VrTimer | None:
    if sys.platform != "win32":
        return None
    return VrTimer(deadline, folder).start()
