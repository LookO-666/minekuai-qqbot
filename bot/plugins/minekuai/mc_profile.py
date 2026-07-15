

"""Minecraft player profile card generation."""

import base64
import datetime

import io

import json
import math

import os

import re

import tempfile

from dataclasses import dataclass, field

from pathlib import Path

from typing import Any



import httpx

from loguru import logger

from PIL import Image, ImageDraw, ImageFont, ImageFilter





NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,16}$")

ASHCON_URL = "https://api.ashcon.app/mojang/v2/user/{name}"

MOJANG_PROFILE_URL = "https://api.mojang.com/users/profiles/minecraft/{name}"

SESSION_URL = "https://sessionserver.mojang.com/session/minecraft/profile/{uuid}"

TIMEOUT = 12.0





@dataclass

class NameHistoryItem:

    name: str

    changed_to_at: str = ""





@dataclass

class PlayerProfile:

    query: str

    name: str

    uuid: str

    skin: Image.Image | None = None

    cape: Image.Image | None = None

    slim: bool = False

    skin_url: str = ""

    cape_url: str = ""

    history: list[NameHistoryItem] = field(default_factory=list)

    source: str = ""





class ProfileError(Exception):

    pass





def validate_name(name: str) -> str:

    name = name.strip()

    if not NAME_RE.fullmatch(name):

        raise ProfileError("玩家名只能包含英文字母、数字、下划线，长度 2-16")

    return name





async def fetch_profile(name: str) -> PlayerProfile:

    """Fetch current profile from Mojang; use third-party data only as a safe supplement."""

    name = validate_name(name)

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as http:

        try:

            profile = await _fetch_mojang(http, name)

        except ProfileError:

            raise

        except Exception as e:

            logger.warning(f"[mc-profile] Mojang failed for {name}, falling back to Ashcon: {e}")

            return await _fetch_ashcon(http, name)



        try:

            history = await _fetch_ashcon_history(http, name, profile)

            if history:

                profile.history = history

                profile.source = "Mojang official + Ashcon history"

        except Exception as e:

            logger.warning(f"[mc-profile] Ashcon history ignored for {name}: {e}")

        return profile





async def _fetch_ashcon_history(http: httpx.AsyncClient, name: str, profile: PlayerProfile) -> list[NameHistoryItem]:

    r = await http.get(ASHCON_URL.format(name=name))

    if r.status_code == 404:

        return []

    r.raise_for_status()

    data = r.json()

    ash_uuid = _dashed_uuid(str(data.get("uuid") or ""))

    if ash_uuid and ash_uuid.lower() != profile.uuid.lower():

        logger.warning(f"[mc-profile] Ashcon UUID mismatch for {name}: {ash_uuid} != {profile.uuid}")

        return []

    ash_name = str(data.get("username") or "")

    if ash_name and ash_name.lower() != profile.name.lower():

        logger.warning(f"[mc-profile] Ashcon stale name for {name}: {ash_name} != {profile.name}")

        return []

    return _parse_history(data.get("username_history") or [])





async def _fetch_ashcon(http: httpx.AsyncClient, name: str) -> PlayerProfile:

    r = await http.get(ASHCON_URL.format(name=name))

    if r.status_code == 404:

        raise ProfileError(f"找不到正版玩家 {name}")

    r.raise_for_status()

    data = r.json()

    textures = data.get("textures") or {}

    skin_info = textures.get("skin") or {}

    cape_info = textures.get("cape") or {}

    skin = await _image_from_info(http, skin_info)

    cape = await _image_from_info(http, cape_info)

    history = _parse_history(data.get("username_history") or [])

    uuid = data.get("uuid") or ""

    profile = PlayerProfile(

        query=name,

        name=data.get("username") or name,

        uuid=uuid,

        skin=skin,

        cape=cape,

        slim=bool(textures.get("slim")),

        skin_url=skin_info.get("url") or "",

        cape_url=cape_info.get("url") or "",

        history=history,

        source="Ashcon + Mojang textures",

    )

    if not profile.uuid or not profile.skin:

        raise ProfileError("资料接口缺少 UUID 或皮肤")

    return profile





async def _fetch_mojang(http: httpx.AsyncClient, name: str) -> PlayerProfile:

    r = await http.get(MOJANG_PROFILE_URL.format(name=name))

    if r.status_code == 404:

        raise ProfileError(f"找不到正版玩家 {name}")

    r.raise_for_status()

    basic = r.json()

    uuid = basic.get("id") or ""

    if not uuid:

        raise ProfileError("Mojang 没返回 UUID")

    r = await http.get(SESSION_URL.format(uuid=uuid), params={"unsigned": "false"})

    r.raise_for_status()

    detail = r.json()

    tex = _extract_textures(detail)

    skin_info = tex.get("SKIN") or {}

    cape_info = tex.get("CAPE") or {}

    skin = await _download_image(http, skin_info.get("url") or "")

    cape = await _download_image(http, cape_info.get("url") or "")

    return PlayerProfile(

        query=name,

        name=detail.get("name") or basic.get("name") or name,

        uuid=_dashed_uuid(uuid),

        skin=skin,

        cape=cape,

        slim=(skin_info.get("metadata") or {}).get("model") == "slim",

        skin_url=skin_info.get("url") or "",

        cape_url=cape_info.get("url") or "",

        history=[],

        source="Mojang official",

    )





async def _image_from_info(http: httpx.AsyncClient, info: dict[str, Any]) -> Image.Image | None:

    raw = info.get("data")

    if raw:

        try:

            return Image.open(io.BytesIO(base64.b64decode(raw))).convert("RGBA")

        except Exception as e:

            logger.warning(f"[mc-profile] decode embedded texture failed: {e}")

    return await _download_image(http, info.get("url") or "")





async def _download_image(http: httpx.AsyncClient, url: str) -> Image.Image | None:

    if not url:

        return None

    if url.startswith("http://textures.minecraft.net/"):

        url = "https://textures.minecraft.net/" + url.split("/texture/", 1)[1].join(["texture/", ""])

    try:

        r = await http.get(url)

        r.raise_for_status()

        return Image.open(io.BytesIO(r.content)).convert("RGBA")

    except Exception as e:

        logger.warning(f"[mc-profile] download image failed {url}: {e}")

        return None





def _extract_textures(detail: dict[str, Any]) -> dict[str, Any]:

    for prop in detail.get("properties") or []:

        if prop.get("name") != "textures":

            continue

        try:

            decoded = base64.b64decode(prop.get("value") or "").decode("utf-8")

            return json.loads(decoded).get("textures") or {}

        except Exception as e:

            logger.warning(f"[mc-profile] parse session textures failed: {e}")

    return {}





def _parse_history(items: list[dict[str, Any]]) -> list[NameHistoryItem]:

    out: list[NameHistoryItem] = []

    for item in items:

        n = item.get("username") or item.get("name")

        if not n:

            continue

        changed = item.get("changed_at") or item.get("changedToAt") or item.get("changed_to_at") or ""

        out.append(NameHistoryItem(str(n), _fmt_changed(changed)))

    return out





def _fmt_changed(value: Any) -> str:

    if not value:

        return "Original"

    if isinstance(value, (int, float)):

        if value > 10_000_000_000:

            value = value / 1000

        return datetime.datetime.fromtimestamp(value).strftime("%Y/%m/%d %H:%M:%S")

    text = str(value).strip()

    if not text:

        return "Original"

    parsed = _parse_datetime_text(text)

    if parsed:

        return parsed.strftime("%Y/%m/%d %H:%M:%S")

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):

        return text.replace("-", "/")

    return text





def _parse_datetime_text(text: str) -> datetime.datetime | None:

    normalized = text.replace("Z", "+00:00")

    try:

        dt = datetime.datetime.fromisoformat(normalized)

    except ValueError:

        return None

    if dt.tzinfo is not None:

        dt = dt.replace(tzinfo=None)

    return dt





def _dashed_uuid(uuid: str) -> str:

    uuid = uuid.replace("-", "")

    if len(uuid) != 32:

        return uuid

    return f"{uuid[:8]}-{uuid[8:12]}-{uuid[12:16]}-{uuid[16:20]}-{uuid[20:]}"





def render_card(profile: PlayerProfile) -> Path:

    if profile.skin is None:

        raise ProfileError("没有拿到皮肤图片")



    W, H = 1280, 1080

    img = Image.new("RGBA", (W, H), (16, 14, 26, 255))

    draw = ImageDraw.Draw(img)



    # Dark game-card style background with soft warm lights.

    for y in range(H):

        r = 12 + int(38 * y / H)

        g = 11 + int(14 * y / H)

        b = 28 + int(24 * (1 - y / H))

        draw.line([(0, y), (W, y)], fill=(r, g, b, 255))

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    gd = ImageDraw.Draw(glow)

    gd.ellipse((920, -140, 1450, 430), fill=(170, 78, 42, 105))

    gd.ellipse((760, 260, 1380, 1180), fill=(150, 80, 30, 82))

    gd.ellipse((-220, 550, 560, 1240), fill=(50, 42, 170, 96))

    gd.rectangle((1048, -80, 1082, 1120), fill=(210, 170, 54, 48))

    glow = glow.filter(ImageFilter.GaussianBlur(42))

    img.alpha_composite(glow)



    font_title = _font(56, bold=True)

    font_head = _font(31, bold=True)

    font_list_head = _font(28, bold=True)

    font_row = _font(25, bold=True)

    font_note = _font(24, bold=True)

    font_empty = _font(32)



    title = profile.query

    if profile.name.lower() != profile.query.lower():

        title = f"{profile.query}->{profile.name}"

    _center_text(draw, title, W // 2, 30, font_title, fill=(255, 188, 28, 255), stroke_width=3)



    left_panel = (36, 130, 632, 664)

    right_panel = (648, 130, 1244, 664)

    _panel(draw, left_panel, fill=(20, 13, 25, 218), outline=(78, 38, 44, 210), radius=14)

    _panel(draw, right_panel, fill=(28, 14, 22, 218), outline=(83, 40, 42, 210), radius=14)

    _panel(draw, (58, 210, 610, 616), fill=(24, 20, 30, 145), outline=(65, 45, 54, 90), radius=12)

    _panel(draw, (670, 210, 1222, 616), fill=(46, 25, 26, 135), outline=(80, 45, 43, 88), radius=12)



    _center_text(draw, "皮肤<Skin>:", 334, 151, font_head, fill=(112, 224, 255, 255), stroke_width=2)

    _center_text(draw, "披风<Cape>:", 946, 151, font_head, fill=(112, 224, 255, 255), stroke_width=2)



    front = render_skin_3d(profile.skin, yaw=-28, scale=11, slim=profile.slim)

    back = render_skin_3d(profile.skin, yaw=152, scale=11, slim=profile.slim)

    img.alpha_composite(front, (82, 218))

    img.alpha_composite(back, (338, 218))



    if profile.cape:

        cape = render_cape(profile.cape, scale=19)

        shadow = Image.new("RGBA", cape.size, (0, 0, 0, 120)).filter(ImageFilter.GaussianBlur(8))

        cx, cy = 946, 405

        img.alpha_composite(shadow, (cx - cape.width // 2 + 8, cy - cape.height // 2 + 8))

        img.alpha_composite(cape, (cx - cape.width // 2, cy - cape.height // 2))

    else:

        draw.rounded_rectangle((788, 238, 1104, 588), radius=8, fill=(17, 18, 26, 230), outline=(96, 58, 102, 170), width=1)

        _center_text(draw, "无披风", 946, 372, font_empty, fill=(232, 232, 238, 255), stroke_width=2)

        _center_text(draw, "No Cape", 946, 414, font_empty, fill=(232, 232, 238, 255), stroke_width=2)



    _draw_text(draw, (52, 688), "历史名称<(仅供参考)>:", font_list_head, fill=(142, 255, 130, 255), stroke_width=2)

    note = "<(仅展示最新4个ID+初始ID)>" if profile.history else "<官方接口未提供历史日期>"

    _draw_text(draw, (880, 688), note, font_note, fill=(105, 224, 255, 255), stroke_width=2)



    rows = _history_rows(profile)

    row_y = 730

    has_history_dates = bool(profile.history)

    for i, item in enumerate(rows):

        y = row_y + i * 62

        _panel(draw, (36, y, 1244, y + 54), fill=(21, 7, 17, 206), outline=(76, 30, 42, 105), radius=10)

        num = len(rows) - i

        name = _mask_name(item.name)

        date = _history_date_label(item.changed_to_at, has_history_dates=has_history_dates)

        _draw_text(draw, (62, y + 13), f"{num}，名称 {name}", font_row, fill=(245, 245, 250, 255), stroke_width=2)

        _draw_text(draw, (862, y + 13), f"日期: {date}", font_row, fill=(245, 245, 250, 255), stroke_width=2)



    out_dir = Path(tempfile.gettempdir()) / "minekuai-mc-cards"

    out_dir.mkdir(parents=True, exist_ok=True)

    out = out_dir / f"mc_{re.sub(r'[^A-Za-z0-9_]+', '_', profile.query)}.png"

    img.convert("RGB").save(out, "PNG", optimize=True)

    return out





def _history_rows(profile: PlayerProfile) -> list[NameHistoryItem]:

    if profile.history:

        if len(profile.history) <= 5:

            return list(reversed(profile.history))

        return list(reversed(profile.history[-4:])) + [profile.history[0]]

    return [NameHistoryItem(profile.name, "Original")]





def _history_date_label(value: str, *, has_history_dates: bool) -> str:

    if not has_history_dates:

        return "官方未提供"

    if not value or value == "Original":

        return "初始ID"

    return value.replace("-", "/")





def _mask_name(name: str) -> str:

    if len(name) <= 8:

        return name

    return f"{name[:2]}~~~~{name[-2:]}"





def _panel(draw: ImageDraw.ImageDraw, box, *, fill, outline, radius: int):

    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=1)



def render_skin_3d(skin: Image.Image, *, yaw: float, scale: int, slim: bool) -> Image.Image:

    skin = _prepare_skin(skin)

    arm_w = 3 if slim else 4



    head = _face_set(

        front=((8, 8, 16, 16), (40, 8, 48, 16)),

        back=((24, 8, 32, 16), (56, 8, 64, 16)),

        left=((16, 8, 24, 16), (48, 8, 56, 16)),

        right=((0, 8, 8, 16), (32, 8, 40, 16)),

        top=((8, 0, 16, 8), (40, 0, 48, 8)),

        bottom=((16, 0, 24, 8), (48, 0, 56, 8)),

    )

    body = _face_set(

        front=((20, 20, 28, 32), (20, 36, 28, 48)),

        back=((32, 20, 40, 32), (32, 36, 40, 48)),

        left=((28, 20, 32, 32), (28, 36, 32, 48)),

        right=((16, 20, 20, 32), (16, 36, 20, 48)),

        top=((20, 16, 28, 20), (20, 32, 28, 36)),

        bottom=((28, 16, 36, 20), (28, 32, 36, 36)),

    )

    right_arm = _face_set(

        front=((44, 20, 44 + arm_w, 32), (44, 36, 44 + arm_w, 48)),

        back=((52, 20, 52 + arm_w, 32), (52, 36, 52 + arm_w, 48)),

        left=((48, 20, 52, 32), (48, 36, 52, 48)),

        right=((40, 20, 44, 32), (40, 36, 44, 48)),

        top=((44, 16, 44 + arm_w, 20), (44, 32, 44 + arm_w, 36)),

        bottom=((48, 16, 48 + arm_w, 20), (48, 32, 48 + arm_w, 36)),

    )

    left_arm = _face_set(

        front=((36, 52, 36 + arm_w, 64), (52, 52, 52 + arm_w, 64)),

        back=((44, 52, 44 + arm_w, 64), (60, 52, 60 + arm_w, 64)),

        left=((40, 52, 44, 64), (56, 52, 60, 64)),

        right=((32, 52, 36, 64), (48, 52, 52, 64)),

        top=((36, 48, 36 + arm_w, 52), (52, 48, 52 + arm_w, 52)),

        bottom=((40, 48, 40 + arm_w, 52), (56, 48, 56 + arm_w, 52)),

    )

    right_leg = _face_set(

        front=((4, 20, 8, 32), (4, 36, 8, 48)),

        back=((12, 20, 16, 32), (12, 36, 16, 48)),

        left=((8, 20, 12, 32), (8, 36, 12, 48)),

        right=((0, 20, 4, 32), (0, 36, 4, 48)),

        top=((4, 16, 8, 20), (4, 32, 8, 36)),

        bottom=((8, 16, 12, 20), (8, 32, 12, 36)),

    )

    left_leg = _face_set(

        front=((20, 52, 24, 64), (4, 52, 8, 64)),

        back=((28, 52, 32, 64), (12, 52, 16, 64)),

        left=((24, 52, 28, 64), (8, 52, 12, 64)),

        right=((16, 52, 20, 64), (0, 52, 4, 64)),

        top=((20, 48, 24, 52), (4, 48, 8, 52)),

        bottom=((24, 48, 28, 52), (8, 48, 12, 52)),

    )



    faces: list[tuple[float, str, list[tuple[float, float]], Image.Image]] = []



    def add_box(bounds, textures):

        for face, verts in _box_faces(bounds).items():

            texture = _compose_face(skin, *textures[face])

            if not texture.getbbox():

                continue

            points = [_project(v, yaw, scale) for v in verts]

            quad = [(x, y) for x, y, _ in points]

            depth = sum(z for _, _, z in points) / 4

            faces.append((depth, face, quad, texture))



    add_box((-4, 0, -4, 4, 8, 4), head)

    add_box((-4, 8, -2, 4, 20, 2), body)

    add_box((-4 - arm_w, 8, -2, -4, 20, 2), right_arm)

    add_box((4, 8, -2, 4 + arm_w, 20, 2), left_arm)

    add_box((-4, 20, -2, 0, 32, 2), right_leg)

    add_box((0, 20, -2, 4, 32, 2), left_leg)



    all_x = [x for _, _, quad, _ in faces for x, _ in quad]

    all_y = [y for _, _, quad, _ in faces for _, y in quad]

    pad = 18

    min_x, max_x = min(all_x), max(all_x)

    min_y, max_y = min(all_y), max(all_y)

    width = int(math.ceil(max_x - min_x + pad * 2))

    height = int(math.ceil(max_y - min_y + pad * 2 + 22))

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))



    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    sd = ImageDraw.Draw(shadow)

    sd.ellipse((width * 0.18, height - 44, width * 0.82, height - 10), fill=(0, 0, 0, 105))

    shadow = shadow.filter(ImageFilter.GaussianBlur(8))

    canvas.alpha_composite(shadow)



    offset_x = pad - min_x

    offset_y = pad - min_y

    draw = ImageDraw.Draw(canvas)

    for _, face, quad, texture in sorted(faces, key=lambda item: item[0]):

        shifted = [(x + offset_x, y + offset_y) for x, y in quad]

        _draw_textured_quad(draw, texture, shifted, _face_shade(face))

    return canvas





def _prepare_skin(skin: Image.Image) -> Image.Image:

    skin = skin.convert("RGBA")

    if skin.width != 64 or skin.height not in (32, 64):

        return skin.resize((64, 64), Image.Resampling.NEAREST)

    if skin.height == 32:

        return _expand_legacy_skin(skin)

    return skin





def _face_set(**kwargs):

    return kwargs





def _compose_face(skin: Image.Image, base_box, overlay_box=None) -> Image.Image:

    face = skin.crop(base_box).convert("RGBA")

    if overlay_box is not None:

        overlay = skin.crop(overlay_box).convert("RGBA")

        if overlay.getbbox():

            face.alpha_composite(overlay)

    return face





def _box_faces(bounds) -> dict[str, list[tuple[float, float, float]]]:

    x1, y1, z1, x2, y2, z2 = bounds

    return {

        "front": [(x1, y1, z2), (x2, y1, z2), (x2, y2, z2), (x1, y2, z2)],

        "back": [(x2, y1, z1), (x1, y1, z1), (x1, y2, z1), (x2, y2, z1)],

        "left": [(x1, y1, z1), (x1, y1, z2), (x1, y2, z2), (x1, y2, z1)],

        "right": [(x2, y1, z2), (x2, y1, z1), (x2, y2, z1), (x2, y2, z2)],

        "top": [(x1, y1, z1), (x2, y1, z1), (x2, y1, z2), (x1, y1, z2)],

        "bottom": [(x1, y2, z2), (x2, y2, z2), (x2, y2, z1), (x1, y2, z1)],

    }





def _project(point, yaw: float, scale: int) -> tuple[float, float, float]:

    x, y, z = point

    angle = math.radians(yaw)

    xr = x * math.cos(angle) + z * math.sin(angle)

    zr = -x * math.sin(angle) + z * math.cos(angle)

    yr = y - zr * 0.23

    return xr * scale, yr * scale, zr





def _face_shade(face: str) -> float:

    return {

        "top": 1.16,

        "front": 1.02,

        "left": 0.88,

        "right": 0.74,

        "back": 0.80,

        "bottom": 0.58,

    }.get(face, 1.0)





def _draw_textured_quad(draw: ImageDraw.ImageDraw, texture: Image.Image, quad, shade: float):

    texture = texture.convert("RGBA")

    w, h = texture.size

    q0, q1, q2, q3 = quad



    def point(u: float, v: float) -> tuple[int, int]:

        x = q0[0] * (1 - u) * (1 - v) + q1[0] * u * (1 - v) + q2[0] * u * v + q3[0] * (1 - u) * v

        y = q0[1] * (1 - u) * (1 - v) + q1[1] * u * (1 - v) + q2[1] * u * v + q3[1] * (1 - u) * v

        return round(x), round(y)



    for py in range(h):

        for px in range(w):

            r, g, b, a = texture.getpixel((px, py))

            if a == 0:

                continue

            color = (min(255, int(r * shade)), min(255, int(g * shade)), min(255, int(b * shade)), a)

            u0, u1 = px / w, (px + 1) / w

            v0, v1 = py / h, (py + 1) / h

            draw.polygon([point(u0, v0), point(u1, v0), point(u1, v1), point(u0, v1)], fill=color)





def render_skin(skin: Image.Image, *, back: bool, scale: int, slim: bool) -> Image.Image:

    skin = skin.convert("RGBA")

    if skin.width != 64 or skin.height not in (32, 64):

        skin = skin.resize((64, 64), Image.Resampling.NEAREST)

    elif skin.height == 32:

        skin = _expand_legacy_skin(skin)



    arm_w = 3 if slim else 4

    canvas = Image.new("RGBA", (16 * scale, 32 * scale), (0, 0, 0, 0))



    def paste(box, x, y, w=None, h=None):

        part = skin.crop(box)

        if w is None:

            w = box[2] - box[0]

        if h is None:

            h = box[3] - box[1]

        part = part.resize((w * scale, h * scale), Image.Resampling.NEAREST)

        canvas.alpha_composite(part, (x * scale, y * scale))



    if not back:

        paste((8, 8, 16, 16), 4, 0, 8, 8)

        paste((40, 8, 48, 16), 4, 0, 8, 8)

        paste((20, 20, 28, 32), 4, 8, 8, 12)

        paste((20, 36, 28, 48), 4, 8, 8, 12)

        paste((44, 20, 44 + arm_w, 32), 1, 8, arm_w, 12)

        paste((44, 36, 44 + arm_w, 48), 1, 8, arm_w, 12)

        paste((36, 52, 36 + arm_w, 64), 12, 8, arm_w, 12)

        paste((52, 52, 52 + arm_w, 64), 12, 8, arm_w, 12)

        paste((4, 20, 8, 32), 4, 20, 4, 12)

        paste((4, 36, 8, 48), 4, 20, 4, 12)

        paste((20, 52, 24, 64), 8, 20, 4, 12)

        paste((4, 52, 8, 64), 8, 20, 4, 12)

    else:

        paste((24, 8, 32, 16), 4, 0, 8, 8)

        paste((56, 8, 64, 16), 4, 0, 8, 8)

        paste((32, 20, 40, 32), 4, 8, 8, 12)

        paste((32, 36, 40, 48), 4, 8, 8, 12)

        paste((52, 20, 52 + arm_w, 32), 1, 8, arm_w, 12)

        paste((52, 36, 52 + arm_w, 48), 1, 8, arm_w, 12)

        paste((44, 52, 44 + arm_w, 64), 12, 8, arm_w, 12)

        paste((60, 52, 60 + arm_w, 64), 12, 8, arm_w, 12)

        paste((12, 20, 16, 32), 4, 20, 4, 12)

        paste((12, 36, 16, 48), 4, 20, 4, 12)

        paste((28, 52, 32, 64), 8, 20, 4, 12)

        paste((12, 52, 16, 64), 8, 20, 4, 12)



    return canvas





def render_cape(cape: Image.Image, *, scale: int) -> Image.Image:

    cape = cape.convert("RGBA")

    if cape.width != 64 or cape.height != 32:

        cape = cape.resize((64, 32), Image.Resampling.NEAREST)



    front = cape.crop((1, 1, 11, 17)).resize((10 * scale, 16 * scale), Image.Resampling.NEAREST)

    back = cape.crop((12, 1, 22, 17)).resize((10 * scale, 16 * scale), Image.Resampling.NEAREST)

    gap = 34

    canvas = Image.new("RGBA", (front.width + gap + back.width, front.height), (0, 0, 0, 0))

    canvas.alpha_composite(front, (0, 0))

    canvas.alpha_composite(back, (front.width + gap, 0))

    return canvas





def _expand_legacy_skin(skin: Image.Image) -> Image.Image:

    out = Image.new("RGBA", (64, 64), (0, 0, 0, 0))

    out.alpha_composite(skin, (0, 0))

    return out





def _fit(image: Image.Image, max_w: int, max_h: int) -> Image.Image:

    img = image.convert("RGBA")

    ratio = min(max_w / img.width, max_h / img.height)

    ratio = max(1, ratio) if img.width < max_w and img.height < max_h else ratio

    return img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))), Image.Resampling.NEAREST)





def _font(size: int, bold: bool = False):

    candidates = []

    if bold:

        candidates.extend([

            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",

            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",

            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",

            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",

        ])

    candidates.extend([

        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",

        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",

        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",

    ])

    for c in candidates:

        if os.path.exists(c):

            return ImageFont.truetype(c, size)

    return ImageFont.load_default()





def _draw_text(draw: ImageDraw.ImageDraw, xy, text: str, font, fill, stroke_width: int = 0):

    draw.text(xy, text, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=(0, 0, 0, 190))



def _center_text(draw: ImageDraw.ImageDraw, text: str, cx: int, y: int, font, fill, stroke_width: int = 0):

    box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)

    draw.text((cx - (box[2] - box[0]) // 2, y), text, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=(0, 0, 0, 190))

