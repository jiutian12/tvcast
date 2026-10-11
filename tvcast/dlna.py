"""DLNA AVTransport / RenderingControl 控制（纯标准库实现 SOAP）。"""

from __future__ import annotations

import re
import urllib.request
from typing import Optional
from xml.sax.saxutils import escape

from .discovery import Device
from .util import upnp_class_for

SOAP_ENV = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body>{body}</s:Body></s:Envelope>"
)


class DlnaError(Exception):
    pass


def build_didl(url: str, title: str, mime: str, upnp_class: str, size: int = 0,
               duration: str = "") -> str:
    """构造 DIDL-Lite 元数据，告诉电视这是什么、从哪拉。

    duration 用 ISO8601（H:MM:SS.mmm）。直播转码流本身没有可读的时长，
    把这个属性写进元数据，电视的进度条才有总时长可显示。
    """
    size_attr = f' size="{size}"' if size > 0 else ""
    dur_attr = f' duration="{duration}"' if duration else ""
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        f"<dc:title>{escape(title)}</dc:title>"
        f"<upnp:class>{upnp_class}</upnp:class>"
        f'<res protocolInfo="http-get:*:{mime}:*"{size_attr}{dur_attr}>{escape(url)}</res>'
        "</item></DIDL-Lite>"
    )


def _soap_call(control_url: str, service_type: str, action: str, args: dict, timeout: float = 8.0) -> str:
    inner = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in args.items())
    body = (
        f'<u:{action} xmlns:u="{service_type}">{inner}</u:{action}>'
    )
    payload = SOAP_ENV.format(body=body).encode("utf-8")
    req = urllib.request.Request(
        control_url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{service_type}#{action}"',
            "User-Agent": "TVCast/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        raise DlnaError(f"{action} 失败: {e}") from e


# --------------------------------------------------------------------------
# 高层动作
# --------------------------------------------------------------------------
def set_uri(dev: Device, url: str, title: str, mime: str, upnp_class: str = "",
            size: int = 0, duration: str = "") -> None:
    metadata = build_didl(url, title, mime, upnp_class or upnp_class_for(title),
                          size, duration)
    _soap_call(dev.control_url, dev.service_type, "SetAVTransportURI", {
        "InstanceID": 0,
        "CurrentURI": url,
        "CurrentURIMetaData": metadata,
    })


def play(dev: Device, speed: str = "1") -> None:
    _soap_call(dev.control_url, dev.service_type, "Play", {
        "InstanceID": 0,
        "Speed": speed,
    })


def pause(dev: Device) -> None:
    _soap_call(dev.control_url, dev.service_type, "Pause", {"InstanceID": 0})


def stop(dev: Device) -> None:
    _soap_call(dev.control_url, dev.service_type, "Stop", {"InstanceID": 0})


def seek(dev: Device, seconds: int) -> None:
    _soap_call(dev.control_url, dev.service_type, "Seek", {
        "InstanceID": 0,
        "Unit": "REL_TIME",
        "Target": _fmt_hms(seconds),
    })


def _fmt_hms(seconds: int) -> str:
    h, rem = divmod(max(0, int(seconds)), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def get_position(dev: Device) -> Optional[tuple[int, int, str]]:
    """返回 (已播放秒, 总时长秒, 播放状态)。失败返回 None。"""
    try:
        resp = _soap_call(dev.control_url, dev.service_type, "GetPositionInfo", {"InstanceID": 0})
    except DlnaError:
        return None
    dur = _find(resp, "TrackDuration")
    pos = _find(resp, "RelTime")
    state = "UNKNOWN"
    try:
        tresp = _soap_call(dev.control_url, dev.service_type, "GetTransportInfo", {"InstanceID": 0})
        state = _find(tresp, "CurrentTransportState") or "UNKNOWN"
    except DlnaError:
        pass
    return _parse_hms(pos), _parse_hms(dur), state


def _find(xml: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
    return m.group(1).strip() if m else ""


def _parse_hms(text: str) -> int:
    m = re.match(r"(\d+):(\d+):(\d+)", text or "")
    if not m:
        return 0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


# --------------------------------------------------------------------------
# 音量
# --------------------------------------------------------------------------
def set_volume(dev: Device, volume: int) -> None:
    if not dev.render_control_url:
        raise DlnaError("该设备不支持音量控制(RenderingControl)")
    _soap_call(dev.render_control_url, dev.render_service_type, "SetVolume", {
        "InstanceID": 0,
        "Channel": "Master",
        "DesiredVolume": max(0, min(100, int(volume))),
    })


def get_volume(dev: Device) -> Optional[int]:
    if not dev.render_control_url:
        return None
    try:
        resp = _soap_call(dev.render_control_url, dev.render_service_type, "GetVolume", {
            "InstanceID": 0,
            "Channel": "Master",
        })
    except DlnaError:
        return None
    # CurrentVolume 是纯数字（"0"~"100"），不是 H:MM:SS —— 之前误用 _parse_hms 解析，
    # 结果永远返回 0，界面上音量条一直是 0。
    raw = _find(resp, "CurrentVolume")
    if not raw:
        return None
    try:
        return max(0, min(100, int(float(raw))))
    except ValueError:
        return None


def fmt_iso_duration(seconds: float) -> str:
    """DLNA 的 res duration 用 ISO8601：H:MM:SS.mmm"""
    s = max(0.0, float(seconds or 0))
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h}:{m:02d}:{sec:06.3f}"


def cast(dev: Device, url: str, title: str, mime: str, upnp_class: str = "",
         size: int = 0, duration: str = "") -> None:
    """一步到位：设置地址 -> 播放。"""
    set_uri(dev, url, title, mime, upnp_class, size, duration)
    play(dev)
