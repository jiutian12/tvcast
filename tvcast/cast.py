"""Chromecast（Google Cast）推送。

安卓电视 / 电视盒子普遍内置 Chromecast built-in。
用 pychromecast 把本地 HTTP 地址投到默认媒体接收器（Default Media Receiver）。

注意（pychromecast 14.x 实测）：
  * Chromecast 对象本身没有 set_volume，音量在 receiver_controller 上；
  * 音量取值范围 0.0~1.0，状态字段是 status.volume_level；
  * play_media 默认 stream_type=LIVE，对本地文件必须显式设成 BUFFERED，
    否则电视按直播处理 —— 拿不到总时长、也不能拖进度。
"""

from __future__ import annotations

from typing import Optional

from .discovery import Device


def _ensure_connected(dev: Device, timeout: float = 10.0):
    c = dev.cast_info
    if c is None:
        raise RuntimeError("该设备未持有 Chromecast 连接对象，请重新扫描")
    # wait() 会在 socket 线程未启动时自动连上；已连上则立即返回
    try:
        c.wait(timeout=timeout)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"连接 Chromecast 失败: {e}") from e
    return c


def cast_media(dev: Device, url: str, title: str, mime: str, image_url: Optional[str] = None) -> None:
    import pychromecast

    c = _ensure_connected(dev)
    mc = c.media_controller
    mc.play_media(
        url,
        mime or "video/mp4",
        title=title or "",
        thumb=image_url,
        stream_type=pychromecast.STREAM_TYPE_BUFFERED,
        autoplay=True,
    )
    mc.block_until_active(timeout=20)


def play(dev: Device) -> None:
    _ensure_connected(dev).media_controller.play()


def pause(dev: Device) -> None:
    _ensure_connected(dev).media_controller.pause()


def stop(dev: Device) -> None:
    _ensure_connected(dev).media_controller.stop()


def seek(dev: Device, seconds: int) -> None:
    _ensure_connected(dev).media_controller.seek(float(seconds))


def set_volume(dev: Device, volume: int) -> None:
    c = _ensure_connected(dev)
    level = max(0.0, min(1.0, volume / 100.0))
    c.socket_client.receiver_controller.set_volume(level)


def status(dev: Device):
    try:
        c = _ensure_connected(dev, timeout=5.0)
        mc = c.media_controller
        st = mc.status
        vol = None
        if c.status is not None:
            vol = getattr(c.status, "volume_level", None)
        return {
            "player_state": getattr(st, "player_state", "UNKNOWN"),
            "current_time": getattr(st, "current_time", 0.0) or 0.0,
            "duration": getattr(st, "duration", 0) or 0,
            "volume": int(round((vol or 0) * 100)),
            "title": getattr(getattr(st, "media_info", None), "title", "") or "",
        }
    except Exception:
        return None


def disconnect(dev: Device) -> None:
    """扫描完 / 退出前释放连接，避免 socket 泄漏。"""
    c = dev.cast_info
    if c is None:
        return
    try:
        c.disconnect(timeout=1)
    except Exception:
        pass
