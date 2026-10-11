"""局域网设备发现。

两条腿走路：
  1) DLNA / UPnP：SSDP M-SEARCH 组播 -> 拿到 LOCATION -> 解析设备描述 XML
     -> 找出 AVTransport / RenderingControl 的 controlURL。
  2) Chromecast：mDNS 发现 _googlecast._tcp.local（安卓电视 / 电视盒子常见内置）。
"""

from __future__ import annotations

import re
import socket
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .util import get_local_ip

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

# 优先 MediaRenderer（能接收推送）
SEARCH_TARGETS = [
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "urn:schemas-upnp-org:device:MediaRenderer:2",
    "urn:schemas-upnp-org:device:MediaRenderer:3",
    "ssdp:all",
]

NS = {
    "d": "urn:schemas-upnp-org:device-1-0",
    "s": "urn:schemas-upnp-org:service-1-0",
}


@dataclass
class Device:
    name: str
    kind: str = "dlna"              # dlna | chromecast
    location: str = ""              # 设备描述 XML 地址
    host: str = ""                  # IP
    model: str = ""
    manufacturer: str = ""
    control_url: str = ""           # AVTransport controlURL（绝对地址）
    render_control_url: str = ""    # RenderingControl controlURL
    service_type: str = "urn:schemas-upnp-org:service:AVTransport:1"
    render_service_type: str = "urn:schemas-upnp-org:service:RenderingControl:1"
    uuid: str = ""
    # chromecast 用
    cast_info: object = None
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.uuid or f"{self.kind}:{self.host}:{self.name}"

    def __str__(self) -> str:
        tag = "DLNA" if self.kind == "dlna" else "Cast"
        model = f" · {self.model}" if self.model and self.model != self.name else ""
        return f"[{tag}] {self.name}{model} ({self.host})"


# --------------------------------------------------------------------------
# SSDP
# --------------------------------------------------------------------------
def _ssdp_probe(targets: List[str], local_ip: str, timeout: float) -> List[str]:
    """一次性发出全部 M-SEARCH，统一收集应答（避免每个 ST 各等一轮超时）。"""
    locations: List[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        except OSError:
            pass
        try:
            sock.bind((local_ip, 0))
        except OSError:
            sock.bind(("", 0))
        sock.settimeout(0.4)

        sent = False
        for st in targets:
            msg = (
                "M-SEARCH * HTTP/1.1\r\n"
                f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
                'MAN: "ssdp:discover"\r\n'
                "MX: 2\r\n"
                f"ST: {st}\r\n"
                "\r\n"
            ).encode("ascii")
            try:
                sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
                sent = True
                time.sleep(0.03)
            except OSError:
                continue
        if not sent:
            return locations

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _addr = sock.recvfrom(65507)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", "ignore")
            for line in text.split("\r\n"):
                if line.lower().startswith("location:"):
                    loc = line.split(":", 1)[1].strip()
                    if loc and loc not in locations:
                        locations.append(loc)
    finally:
        sock.close()
    return locations


def _fetch_xml(url: str, timeout: float = 3.0) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TVCast/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(512 * 1024)
        return raw.decode("utf-8", "ignore")
    except Exception:
        return None


def _abs(base: str, rel: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, rel)


def _parse_device(location: str) -> Optional[Device]:
    xml = _fetch_xml(location)
    if not xml:
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None

    # 设备主体信息
    dev = root.find(".//d:device", NS)
    if dev is None:
        return None

    def t(tag: str, default: str = "") -> str:
        el = dev.find(f"d:{tag}", NS)
        return (el.text or "").strip() if el is not None and el.text else default

    name = t("friendlyName") or t("modelName") or "未知设备"
    model = t("modelName")
    manufacturer = t("manufacturer")

    d = Device(
        name=name,
        kind="dlna",
        location=location,
        host=_host_of(location),
        model=model,
        manufacturer=manufacturer,
        uuid=t("UDN"),
    )

    # 服务列表，找 AVTransport / RenderingControl
    # 注意：serviceList 可能出现在根设备的 device 下，也可能在嵌套 device 里
    for devnode in root.iter():
        if not devnode.tag.endswith("}device") and devnode.tag != "device":
            continue
        sl = devnode.find("d:serviceList", NS)
        if sl is None:
            continue
        for svc in sl.findall("d:service", NS):
            st_el = svc.find("d:serviceType", NS)
            cu_el = svc.find("d:controlURL", NS)
            if st_el is None or cu_el is None:
                continue
            st = (st_el.text or "").strip()
            cu = (cu_el.text or "").strip()
            if "AVTransport" in st:
                d.control_url = _abs(location, cu)
                d.service_type = st
            elif "RenderingControl" in st:
                d.render_control_url = _abs(location, cu)
                d.render_service_type = st

    if not d.control_url:
        return None
    return d


def _host_of(location: str) -> str:
    m = re.match(r"https?://([^/:]+)", location)
    return m.group(1) if m else ""


def discover_dlna(local_ip: str, timeout: float = 3.0, log=None) -> List[Device]:
    seen: Dict[str, Device] = {}
    locations = _ssdp_probe(SEARCH_TARGETS, local_ip, timeout)
    if log:
        log(f"SSDP 收到 {len(locations)} 个描述地址")

    for loc in locations:
        dev = _parse_device(loc)
        if dev is None:
            if log:
                log(f"  跳过（非 MediaRenderer 或描述解析失败）: {loc}")
            continue
        if dev.host == local_ip:
            if log:
                log(f"  跳过（本机自身）: {dev.name}")
            continue
        if dev.key in seen:
            continue
        seen[dev.key] = dev
        if log:
            log(f"  发现 DLNA 渲染器: {dev}")
    return list(seen.values())


# --------------------------------------------------------------------------
# Chromecast (mDNS)
# --------------------------------------------------------------------------
def discover_chromecast(timeout: float = 4.0, log=None) -> List[Device]:
    try:
        import pychromecast
    except Exception:
        if log:
            log("未安装 pychromecast，跳过 Chromecast 发现")
        return []

    devices: List[Device] = []
    casts = []
    try:
        # pychromecast 14.x：阻塞式发现，返回 (cast 列表, browser)
        casts, browser = pychromecast.get_chromecasts(timeout=timeout)
        try:
            browser.stop_discovery()
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"Chromecast 发现失败: {e}")
        return []

    for c in casts:
        try:
            info = c.cast_info
            name = getattr(info, "friendly_name", None) or c.name
            uuid = str(getattr(info, "uuid", "") or "")
            host = getattr(info, "host", "") or ""
            model = getattr(info, "model_name", "") or ""
        except Exception:
            name, uuid, host, model = getattr(c, "name", "未知设备"), "", "", ""
        d = Device(name=name, kind="chromecast", host=host, model=model,
                   uuid=f"cast:{uuid}", cast_info=c)
        devices.append(d)
        if log:
            log(f"  发现 Chromecast: {d}")
    return devices


def discover_all(local_ip: Optional[str] = None, log=None, deep: bool = False) -> List[Device]:
    """并发跑 DLNA 与 Chromecast 发现。"""
    local_ip = local_ip or get_local_ip()
    results: List[Device] = []
    lock = threading.Lock()

    def run(fn, *a, **kw):
        try:
            r = fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            if log:
                log(f"发现过程异常: {e}")
            r = []
        with lock:
            results.extend(r)

    t1 = threading.Thread(target=run, args=(discover_dlna, local_ip), kwargs={"timeout": 3.0, "log": log})
    t2 = threading.Thread(target=run, args=(discover_chromecast,), kwargs={"timeout": 4.0, "log": log})
    t1.start(); t2.start()
    t1.join(); t2.join()

    # 去重（按 key）
    uniq: Dict[str, Device] = {}
    for d in results:
        uniq.setdefault(d.key, d)
    return list(uniq.values())
