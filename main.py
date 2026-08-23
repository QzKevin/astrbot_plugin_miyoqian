# -*- coding: utf-8 -*-
"""
astrbot_plugin_miyoqian
米游社游戏每日签到插件（AstrBot）

基于 astrbot_plugin_mihoyo_signin 的功能主体（多账号绑定 / 扫码登录 / 每日自动签到 /
状态查询 / 一键签到），对齐 astrbot_plugin_miyoushe_sign 在游戏签到上的优势：
- DS 签名 salt 使用新版网页端 salt（G1ktdwFL4IyGkHuuWSmz0wUe9Db9scyK，DS v1）
- 原神/绝区零请求头携带 x-rpc-signgame（hk4e / zzz），Origin/Referer 指向 act.mihoyo.com，
  并带 x-rpc-channel: miyousheluodi，行为更贴近新版 App 签到
- 同一游戏下账号的全部角色逐个签到（不再只签列表第一个角色）
- 识别 1034 验证码码并单独提示；登录失效码覆盖 -100 / -101 / 10001 / 1008 / 10103 / 10104
- 基于以上优势，可修复原神/绝区零的签到 API 异常问题

功能：
- 「签到」/「打卡」一键签到当前账号的全部游戏
- 「米游社 绑定 <cookie>」绑定米游社 Cookie
- 「米游社 扫码」扫码登录自动绑定
- 「米游社 查询 [游戏]」查询签到状态（支持多角色）
- 「米游社 我的」查看所有绑定账号
- 「米游社 切换 <序号>」切换默认账号（支持多账号）
- 「米游社 解绑 [全部]」解绑账号
- 每日自动签到（时间可在 WebUI 配置）

支持游戏：原神、崩坏：星穹铁道、崩坏3、绝区零、崩坏学园2、未定事件簿（国服）
"""

import asyncio
import hashlib
import json
import os
import random
import string
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

# ============================================================
# 常量区
# ============================================================

# 游戏信息: game_biz -> (游戏名, act_id, API 根地址, 签到路径, 签名游戏标识)
# act_id 为 2024 年 6 月后国服通用版本
# signgame: 原神/绝区零的新版签到 API 需要 x-rpc-signgame 请求头（对齐 miyoushe_sign）
GAMES = {
    "hk4e_cn": {"name": "原神", "act_id": "e202311201442471",
                "api": "https://api-takumi.mihoyo.com", "path": "luna", "signgame": "hk4e"},
    "hkrpg_cn": {"name": "崩坏：星穹铁道", "act_id": "e202304121516551",
                 "api": "https://api-takumi.mihoyo.com", "path": "luna", "signgame": None},
    "bh3_cn": {"name": "崩坏3", "act_id": "e202306201626331",
               "api": "https://api-takumi.mihoyo.com", "path": "luna", "signgame": None},
    "nap_cn": {"name": "绝区零", "act_id": "e202406242138391",
               "api": "https://act-nap-api.mihoyo.com", "path": "luna/zzz", "signgame": "zzz"},
    "bh2_cn": {"name": "崩坏学园2", "act_id": "e202203291431091",
               "api": "https://api-takumi.mihoyo.com", "path": "luna", "signgame": None},
    "nxx_cn": {"name": "未定事件簿", "act_id": "e202202251749321",
               "api": "https://api-takumi.mihoyo.com", "path": "luna", "signgame": None},
}

# 游戏别名 -> game_biz
GAME_ALIASES = {
    "原神": "hk4e_cn", "genshin": "hk4e_cn", "ys": "hk4e_cn", "hk4e": "hk4e_cn",
    "星穹铁道": "hkrpg_cn", "崩铁": "hkrpg_cn", "崩坏星穹铁道": "hkrpg_cn", "星铁": "hkrpg_cn",
    "starrail": "hkrpg_cn", "sr": "hkrpg_cn", "hkrpg": "hkrpg_cn",
    "崩坏3": "bh3_cn", "崩坏三": "bh3_cn", "崩3": "bh3_cn", "honkai3": "bh3_cn", "bh3": "bh3_cn",
    "绝区零": "nap_cn", "zzz": "nap_cn", "nap": "nap_cn",
    "崩坏学园2": "bh2_cn", "崩坏2": "bh2_cn", "崩2": "bh2_cn", "honkai2": "bh2_cn", "bh2": "bh2_cn",
    "未定事件簿": "nxx_cn", "未定": "nxx_cn", "themis": "nxx_cn", "nxx": "nxx_cn",
}

PASSPORT_API = "https://passport-api.miyoushe.com"
APP_ID = "bll8iq97cem8"  # 米游社网页端 app_id
BINDING_API = "https://api-takumi.mihoyo.com/binding/api/getUserGameRolesByCookie"
STOKEN_CTOKEN_API = "https://api-takumi.mihoyo.com/auth/api/getCookieAccountInfoBySToken"
STOKEN_LTOKEN_API = "https://api-takumi.mihoyo.com/auth/api/getLTokenBySToken"

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 12; Unspecified Device) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/103.0.5060.129 Mobile Safari/537.36 "
    "miHoYoBBS/{ver}"
)

# DS 签名 salt（DS v1，对齐 miyoushe_sign 的新版网页端 salt，可被 WebUI 配置覆盖）
WEB_SALT_DEFAULT = "G1ktdwFL4IyGkHuuWSmz0wUe9Db9scyK"

# 通用返回码
RETCODE_ALREADY_SIGNED = -5003   # 已签到
RETCODE_CAPTCHA = 1034           # 触发验证码
# 登录失效 / 凭证无效
LOGIN_INVALID_CODES = (-100, -101, 10001, 1008, 10103, 10104)
BJT = timezone(timedelta(hours=8))
SCHEDULER_POLL_SECONDS = 30
SCHEDULER_DUE_GRACE_SECONDS = 90


# ============================================================
# 工具函数
# ============================================================

def _random_text(n: int) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _gen_ds(salt: str) -> str:
    """生成米游社 DS 签名: 时间戳,随机串,md5"""
    t = str(int(time.time()))
    r = _random_text(6)
    c = hashlib.md5(f"salt={salt}&t={t}&r={r}".encode("utf-8")).hexdigest()
    return f"{t},{r},{c}"


def _cookie_value(cookie: str, key: str) -> str:
    """从 cookie 字符串中取指定键的值"""
    for part in cookie.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() == key:
                return v.strip()
    return ""


def _assemble_cookie(set_cookies) -> str:
    """从 Set-Cookie 响应头列表组装 cookie 字符串"""
    parts = {}
    for sc in set_cookies or []:
        first = sc.split(";", 1)[0].strip()
        if "=" in first:
            k, v = first.split("=", 1)
            parts[k.strip()] = v.strip()
    return "; ".join(f"{k}={v}" for k, v in parts.items())


def _as_role_list(role_data):
    """把账号里某个游戏的角色数据规范成列表（兼容旧版单角色 dict 存储）"""
    if role_data is None:
        return []
    if isinstance(role_data, dict):
        return [role_data]
    if isinstance(role_data, list):
        return [r for r in role_data if isinstance(r, dict)]
    return []


def _is_today_signed(info: dict) -> bool:
    """判断今日是否已签到，优先使用接口明确返回的 is_sign。"""
    if not isinstance(info, dict):
        return False
    is_sign = info.get("is_sign")
    if isinstance(is_sign, bool):
        return is_sign
    if isinstance(is_sign, (int, float)):
        return bool(is_sign)
    if isinstance(is_sign, str):
        normalized = is_sign.strip().lower()
        if normalized in ("true", "1", "yes"):
            return True
        if normalized in ("false", "0", "no"):
            return False

    # 兼容旧响应字段：只有在接口没有返回 is_sign 时才用它兜底。
    missing = info.get("sign_cnt_missing")
    if missing is None:
        return False
    try:
        return int(missing) == 0
    except (TypeError, ValueError):
        return False


# ============================================================
# 数据存储
# ============================================================

class UserStore:
    """用户绑定数据存储（JSON 文件，存于 AstrBot data 目录）"""

    def __init__(self, path: str):
        self.path = path
        self.data: dict = {}
        self._lock = asyncio.Lock()
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.data = loaded
        except Exception as e:
            logger.error(f"读取用户数据失败: {e}")
            self.data = {}

    async def save(self):
        try:
            async with self._lock:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
        except Exception as e:
            logger.error(f"保存用户数据失败: {e}")


# ============================================================
# 插件主类
# ============================================================

class MihoyoSigninPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 数据目录：官方推荐 data/plugin_data/<插件名>（更新/重装插件不会丢失数据）
        try:
            base = str(StarTools.get_data_dir())
        except Exception as e:
            logger.warning(f"获取插件数据目录失败，退回插件目录: {e}")
            base = os.path.dirname(os.path.abspath(__file__))
        self.data_dir = os.path.join(base, "mihoyo_signin")
        os.makedirs(self.data_dir, exist_ok=True)

        self._migrate_legacy_files()

        self.store = UserStore(os.path.join(self.data_dir, "users.json"))
        self._migrate_legacy_data()
        self._qr_tasks: dict = {}      # sender -> 扫码轮询任务
        self._sched_task = None        # 定时签到任务
        self._sign_lock = asyncio.Lock()
        self._device_id = self._load_device_id()
        self._apply_act_id_override()

    def _migrate_legacy_files(self):
        """旧版本数据迁移：插件目录 mihoyo_signin/ -> data/plugin_data 新位置"""
        try:
            import shutil
            legacy_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mihoyo_signin")
            for fname in ("users.json", "device_id.txt"):
                legacy = os.path.join(legacy_dir, fname)
                target = os.path.join(self.data_dir, fname)
                if os.path.exists(legacy) and not os.path.exists(target):
                    shutil.copy2(legacy, target)
                    logger.info(f"已迁移旧数据文件 {fname} 到新数据目录")
        except Exception as e:
            logger.warning(f"迁移旧数据失败: {e}")

    def _apply_act_id_override(self):
        """应用 WebUI 配置的 act_id 覆盖（米游社更换活动 ID 时无需改代码）"""
        try:
            overrides = self.config.get("act_id_override") or {}
            if isinstance(overrides, dict):
                for biz, aid in overrides.items():
                    if biz in GAMES and aid and isinstance(aid, str):
                        GAMES[biz]["act_id"] = aid.strip()
                        logger.info(f"已应用 {GAMES[biz]['name']} 的 act_id 覆盖: {aid.strip()}")
        except Exception as e:
            logger.warning(f"应用 act_id 覆盖失败: {e}")

    def _migrate_legacy_data(self):
        """v1 单账号数据 -> v2 多账号结构迁移"""
        changed = False
        for sender, user in self.store.data.items():
            if isinstance(user, dict) and "cookie" in user and "accounts" not in user:
                user["accounts"] = [{
                    "cookie": user.get("cookie", ""),
                    "bbs_uid": user.get("bbs_uid", ""),
                    "nickname": user.get("nickname", ""),
                    "roles": user.get("roles", {}),
                    "device_id": user.get("device_id", ""),
                    "bound_at": user.get("bound_at", 0),
                }]
                user["active_index"] = 0
                for k in ("cookie", "bbs_uid", "nickname", "roles", "device_id", "bound_at"):
                    user.pop(k, None)
                changed = True
        if changed:
            try:
                with open(self.store.path, "w", encoding="utf-8") as f:
                    json.dump(self.store.data, f, ensure_ascii=False, indent=2)
                logger.info("已自动迁移旧版绑定数据为多账号格式")
            except Exception as e:
                logger.error(f"迁移数据保存失败: {e}")

    # --------------------------------------------------------
    # 基础工具
    # --------------------------------------------------------

    def _load_device_id(self) -> str:
        """全局固定设备 ID（同一设备身份，接口风控更稳）"""
        p = os.path.join(self.data_dir, "device_id.txt")
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    d = f.read().strip()
                    if d:
                        return d
        except Exception:
            pass
        d = str(uuid.uuid4())
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(d)
        except Exception:
            pass
        return d

    def _headers(self, cookie: str = "", ds: bool = True, game_biz: str = None) -> dict:
        """构造米游社 API 通用请求头

        game_biz 非空时按游戏签到请求处理（对齐 miyoushe_sign）：
        - Origin/Referer 指向 act.mihoyo.com
        - 带 x-rpc-channel: miyousheluodi
        - 原神/绝区零额外携带 x-rpc-signgame（hk4e / zzz）
        否则（绑定查询 / 通行证 token 接口）使用 webstatic 头。
        """
        ver = str(self.config.get("app_version", "2.109.0"))
        is_game = game_biz in GAMES
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": USER_AGENT.format(ver=ver),
            "x-rpc-app_version": ver,
            "x-rpc-client_type": str(self.config.get("client_type", "5")),
            "Origin": "https://act.mihoyo.com" if is_game else "https://webstatic.mihoyo.com",
            "Referer": "https://act.mihoyo.com/" if is_game else "https://webstatic.mihoyo.com/",
            "Accept-Language": "zh-CN,en-US;q=0.8",
            "X-Requested-With": "com.mihoyo.hyperion",
            "Connection": "keep-alive",
            "x-rpc-device_id": self._device_id,
        }
        if is_game:
            headers["x-rpc-channel"] = "miyousheluodi"
            sg = GAMES[game_biz].get("signgame")
            if sg:
                headers["x-rpc-signgame"] = sg
        if cookie:
            headers["Cookie"] = cookie
        if ds:
            headers["DS"] = _gen_ds(str(self.config.get("ds_salt", WEB_SALT_DEFAULT)))
        return headers

    async def _send(self, umo: str, text: str):
        """向指定会话推送消息（用于定时任务 / 扫码轮询）"""
        try:
            await self.context.send_message(umo, MessageChain().message(text))
        except Exception as e:
            logger.error(f"推送消息失败: {e}")

    def _active_account(self, sender: str):
        """获取用户的当前默认账号（account dict）"""
        user = self.store.data.get(sender)
        if not user:
            return None
        accounts = user.get("accounts") or []
        if not accounts:
            return None
        try:
            idx = int(user.get("active_index", 0) or 0)
        except (TypeError, ValueError):
            idx = 0
        if idx < 0 or idx >= len(accounts):
            idx = 0
        return accounts[idx]

    def _get_user_device(self, sender: str) -> str:
        """扫码用设备 ID：优先当前账号的，无则新建"""
        acc = self._active_account(sender)
        if acc and acc.get("device_id"):
            return acc["device_id"]
        return str(uuid.uuid4())

    # --------------------------------------------------------
    # 米游社 API
    # --------------------------------------------------------

    async def _request(self, method: str, url: str, cookie: str = "",
                       params: dict = None, body: dict = None,
                       game_biz: str = None, ds: bool = True) -> dict:
        """带重试的请求封装；game_biz 非空时使用游戏签到请求头"""
        retries = max(1, int(self.config.get("api_retries", 3)))
        last_err = None
        for i in range(retries):
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.request(
                        method, url,
                        params=params, json=body,
                        headers=self._headers(cookie, ds=ds, game_biz=game_biz),
                    )
                try:
                    return resp.json()
                except Exception:
                    last_err = f"响应解析失败 (HTTP {resp.status_code})"
            except Exception as e:
                last_err = str(e)
            if i < retries - 1:
                await asyncio.sleep(1 + i)
        raise RuntimeError(last_err or "网络请求失败")

    async def _get_roles(self, cookie: str, game_biz: str):
        """获取账号在某游戏下的全部角色信息（不再只取第一个）"""
        j = await self._request("GET", BINDING_API, cookie=cookie,
                                params={"game_biz": game_biz}, game_biz=game_biz)
        if j.get("retcode") != 0:
            return None, j.get("message", f"错误码 {j.get('retcode')}")
        lst = (j.get("data") or {}).get("list") or []
        if not lst:
            return None, "该账号没有此游戏的绑定角色"
        roles = []
        for r in lst:
            roles.append({
                "game_uid": str(r.get("game_uid")),
                "region": r.get("region"),
                "nickname": r.get("nickname"),
                "level": r.get("level"),
                "region_name": r.get("region_name", ""),
            })
        return roles, None

    async def _fetch_all_roles(self, cookie: str) -> dict:
        """拉取账号在所有支持游戏下的角色（绑定 / 扫码时调用）"""
        roles = {}
        for biz in GAMES:
            try:
                role_list, err = await self._get_roles(cookie, biz)
                if role_list and not err:
                    roles[biz] = role_list
            except Exception as e:
                logger.warning(f"拉取 {GAMES[biz]['name']} 角色失败: {e}")
        return roles

    def _sign_urls(self, game_biz: str):
        """返回 (签到URL, 信息URL, 奖励URL)"""
        g = GAMES[game_biz]
        base = f"{g['api']}/event/{g['path']}"
        return (
            f"{base}/sign",
            f"{base}/info",
            f"{base}/home",
        )

    async def _get_awards(self, cookie: str, game_biz: str) -> list:
        """获取签到奖励列表（home 接口，用于无 award 回执时展示奖励；对齐 miyoushe_sign）"""
        g = GAMES[game_biz]
        _, _, home_url = self._sign_urls(game_biz)
        try:
            j = await self._request("GET", home_url, cookie=cookie,
                                    params={"act_id": g["act_id"]}, game_biz=game_biz)
            if j.get("retcode") == 0:
                awards = (j.get("data") or {}).get("awards") or []
                return awards if isinstance(awards, list) else []
        except Exception as e:
            logger.warning(f"获取 {g['name']} 签到奖励列表失败: {e}")
        return []

    def _describe_award(self, awards: list, index: int) -> str:
        """按累计签到天数映射奖励名称"""
        if not awards:
            return ""
        index = max(0, min(int(index), len(awards) - 1))
        award = awards[index]
        return f"「{award.get('name', '未知')}」x{award.get('cnt', '?')}"

    async def _sign_one(self, account: dict, game_biz: str) -> str:
        """对单个游戏执行签到（该游戏下的全部角色），返回结果文本"""
        g = GAMES[game_biz]
        cookie = account.get("cookie", "")
        roles_map = account.setdefault("roles", {})
        role_list = _as_role_list(roles_map.get(game_biz))

        # 没有角色信息则实时拉取
        if not role_list:
            role_list, err = await self._get_roles(cookie, game_biz)
            if not role_list:
                return f"【{g['name']}】⚠️ {err}"
            roles_map[game_biz] = role_list

        sign_url, info_url, _ = self._sign_urls(game_biz)
        params_base = {"lang": "zh-cn", "act_id": g["act_id"]}

        lines = []
        for role in role_list:
            region = role.get("region") or ""
            game_uid = str(role.get("game_uid") or "")
            region_name = role.get("region_name") or ""
            nickname = role.get("nickname") or game_uid
            if len(role_list) > 1:
                label = f"{region_name or region} · {nickname}({game_uid})"
            else:
                label = f"{region_name or region} · {game_uid}" if region_name else game_uid

            # 1. 查签到信息
            try:
                j = await self._request(
                    "GET", info_url, cookie=cookie,
                    params={**params_base, "region": region, "uid": game_uid},
                    game_biz=game_biz)
            except Exception as e:
                lines.append(f"❌ {label} 网络错误：{e}")
                continue
            retcode = j.get("retcode")
            if retcode == RETCODE_CAPTCHA:
                lines.append(f"⚠️ {label} 触发验证码（1034），本次跳过，请稍后在米游社手动签到")
                continue
            if retcode in LOGIN_INVALID_CODES:
                return f"【{g['name']}】❌ Cookie 已失效（{retcode}），请重新绑定或扫码登录"
            if retcode != 0:
                lines.append(f"❌ {label} 查询失败：{j.get('message', retcode)}")
                continue

            info = j.get("data") or {}
            if info.get("first_bind"):
                lines.append(f"⚠️ {label} 首次绑定，请先在米游社手动签到一次")
                continue
            total_sign_day = int(info.get("total_sign_day") or 0)
            already = _is_today_signed(info)
            if already:
                lines.append(f"✅ {label} 今日已签到（本月累计 {total_sign_day} 天）")
                continue

            # 2. 执行签到
            try:
                j2 = await self._request(
                    "POST", sign_url, cookie=cookie,
                    body={"act_id": g["act_id"], "region": region, "uid": game_uid},
                    game_biz=game_biz)
            except Exception as e:
                lines.append(f"❌ {label} 网络错误：{e}")
                continue

            rc2 = j2.get("retcode")
            if rc2 == 0:
                sign_data = j2.get("data") or {}
                if str(sign_data.get("success") or "") == "1":
                    lines.append(f"⚠️ {label} 触发验证码/风控，本次未完成签到，请稍后在米游社手动签到")
                    continue
                award = sign_data.get("award")
                if award:
                    name, cnt = award.get("name", "?"), award.get("cnt", 1)
                    lines.append(
                        f"✅ {label} 签到成功！今日奖励：「{name}」x{cnt}（本月累计 {total_sign_day + 1} 天）")
                else:
                    # 无 award 回执时回退到 home 奖励列表按天数映射（对齐 miyoushe_sign）
                    awards = await self._get_awards(cookie, game_biz)
                    award_str = self._describe_award(awards, total_sign_day)
                    lines.append(
                        f"✅ {label} 签到成功！今日奖励 {award_str}（本月累计 {total_sign_day + 1} 天）"
                        if award_str else
                        f"✅ {label} 签到成功！（本月累计 {total_sign_day + 1} 天）")
            elif rc2 == RETCODE_ALREADY_SIGNED:
                lines.append(f"✅ {label} 今日已签到（本月累计 {total_sign_day} 天）")
            elif rc2 == RETCODE_CAPTCHA:
                lines.append(f"⚠️ {label} 触发验证码（1034），本次跳过，请稍后在米游社手动签到")
            elif rc2 in LOGIN_INVALID_CODES:
                return f"【{g['name']}】❌ Cookie 已失效（{rc2}），请重新绑定或扫码登录"
            else:
                lines.append(f"❌ {label} 签到失败：{j2.get('message', rc2)}")
            await asyncio.sleep(0.8)  # 轻微限速，防风控

        if not lines:
            return f"【{g['name']}】⚠️ 无角色"
        return f"【{g['name']}】\n" + "\n".join(f"  {ln}" for ln in lines)

    # --------------------------------------------------------
    # 扫码登录
    # --------------------------------------------------------

    async def _create_qr(self, device_id: str):
        """获取登录二维码，返回 (url, ticket)"""
        ver = str(self.config.get("app_version", "2.109.0"))
        headers = {
            "x-rpc-app_id": APP_ID,
            "x-rpc-device_id": device_id,
            "x-rpc-client_type": "4",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT.format(ver=ver),
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{PASSPORT_API}/account/ma-cn-passport/web/createQRLogin",
                headers=headers, json={},
            )
            j = resp.json()
        if j.get("retcode") != 0:
            raise RuntimeError(j.get("message", f"错误码 {j.get('retcode')}"))
        data = j.get("data") or {}
        return data.get("url"), data.get("ticket")

    async def _query_qr(self, ticket: str, device_id: str):
        """查询二维码状态，返回 (json, resp)"""
        ver = str(self.config.get("app_version", "2.109.0"))
        headers = {
            "x-rpc-app_id": APP_ID,
            "x-rpc-device_id": device_id,
            "x-rpc-client_type": "4",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT.format(ver=ver),
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{PASSPORT_API}/account/ma-cn-passport/web/queryQRLoginStatus",
                headers=headers, json={"ticket": ticket},
            )
            return resp.json(), resp

    async def _complete_cookie(self, cookie: str) -> str:
        """用 stoken 补全缺失的 cookie_token / ltoken"""
        stoken = _cookie_value(cookie, "stoken") or _cookie_value(cookie, "stoken_v2")
        if not stoken:
            return cookie
        try:
            if not (_cookie_value(cookie, "cookie_token") or _cookie_value(cookie, "cookie_token_v2")):
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(STOKEN_CTOKEN_API,
                                         headers=self._headers(f"stoken={stoken}", ds=False))
                    d = (r.json().get("data") or {})
                    ct = d.get("cookie_token")
                    if ct:
                        cookie += f"; cookie_token={ct}"
        except Exception as e:
            logger.warning(f"补全 cookie_token 失败: {e}")
        try:
            if not (_cookie_value(cookie, "ltoken") or _cookie_value(cookie, "ltoken_v2")):
                mid = _cookie_value(cookie, "mid") or _cookie_value(cookie, "mid_v2")
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(STOKEN_LTOKEN_API,
                                         params={"stoken": stoken},
                                         headers=self._headers(f"stoken={stoken}; mid={mid}", ds=False))
                    d = (r.json().get("data") or {})
                    lt = d.get("ltoken") or d.get("token")
                    if lt:
                        cookie += f"; ltoken={lt}"
        except Exception as e:
            logger.warning(f"补全 ltoken 失败: {e}")
        return cookie

    async def _poll_qr(self, ticket: str, device_id: str, sender: str, umo: str, timeout: int):
        """轮询二维码状态，确认后自动绑定"""
        last_status = None
        start = time.time()
        try:
            while time.time() - start < timeout:
                await asyncio.sleep(3)
                try:
                    j, resp = await self._query_qr(ticket, device_id)
                except Exception as e:
                    logger.error(f"扫码轮询出错: {e}")
                    continue

                retcode = j.get("retcode")
                if retcode in (-3501, -3505):  # 过期 / 取消
                    await self._send(umo, "二维码已失效或已取消，需要重新登录请再发一次「米游社 扫码」~")
                    return
                status = (j.get("data") or {}).get("status")
                if status == "Scanned" and last_status != "Scanned":
                    await self._send(umo, "📱 已扫码！请在手机上确认登录~")
                if status == "Confirmed":
                    set_cookies = resp.headers.get_list("set-cookie")
                    cookie = _assemble_cookie(set_cookies)
                    if not cookie:
                        await self._send(umo, "⚠️ 扫码确认成功，但未获取到完整 Cookie，请改用「米游社 绑定 <cookie>」手动绑定~")
                        return
                    cookie = await self._complete_cookie(cookie)
                    ok, msg, _ = await self._validate_and_bind(sender, cookie, umo, device_id)
                    if ok:
                        await self._send(umo, f"🎉 绑定成功！{msg}\n之后每天会自动签到，也可以直接发送「签到」一键签到~")
                    else:
                        await self._send(umo, f"❌ 绑定失败：{msg}\n可尝试「米游社 绑定 <cookie>」手动绑定。")
                    return
                last_status = status
            await self._send(umo, "⏰ 二维码已过期，需要重新登录请再发一次「米游社 扫码」~")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"扫码轮询异常: {e}")
            try:
                await self._send(umo, f"扫码登录过程出错：{e}")
            except Exception:
                pass
        finally:
            self._qr_tasks.pop(sender, None)

    # --------------------------------------------------------
    # 绑定
    # --------------------------------------------------------

    async def _validate_and_bind(self, sender: str, cookie: str, umo: str,
                                 device_id: str = None):
        """验证 cookie 并新增/更新绑定账号，返回 (ok, msg, index)"""
        cookie = cookie.strip()
        if not cookie:
            return False, "Cookie 为空", -1
        try:
            roles = await self._fetch_all_roles(cookie)
        except Exception as e:
            return False, f"验证 Cookie 时网络出错：{e}", -1
        if not roles:
            return False, "Cookie 无效或账号下没有任何游戏角色（请确认 Cookie 是 user.mihoyo.com 登录后的完整 Cookie）", -1

        first_roles = next(iter(roles.values()))
        first_role = first_roles[0] if isinstance(first_roles, list) else first_roles
        bbs_uid = (_cookie_value(cookie, "ltuid") or _cookie_value(cookie, "ltuid_v2")
                   or _cookie_value(cookie, "account_id") or "")
        if not bbs_uid:
            bbs_uid = str(first_role.get("game_uid", ""))

        user = self.store.data.setdefault(sender, {
            "accounts": [], "active_index": 0,
            "umo": umo, "last_auto_sign_date": "",
        })
        user["umo"] = umo

        # 相同 bbs_uid 的账号 -> 更新已有绑定；否则新增
        for i, acc in enumerate(user["accounts"]):
            if acc.get("bbs_uid") == bbs_uid:
                acc.update({
                    "cookie": cookie,
                    "nickname": first_role.get("nickname", ""),
                    "roles": roles,
                    "bound_at": int(time.time()),
                })
                user["active_index"] = i
                await self.store.save()
                games_str = "、".join(GAMES[b]["name"] for b in roles)
                return True, f"已更新账号 {bbs_uid}（{first_role.get('nickname', '')}），识别游戏：{games_str}", i

        acc = {
            "cookie": cookie,
            "bbs_uid": bbs_uid,
            "nickname": first_role.get("nickname", ""),
            "roles": roles,
            "device_id": device_id or str(uuid.uuid4()),
            "bound_at": int(time.time()),
        }
        user["accounts"].append(acc)
        user["active_index"] = len(user["accounts"]) - 1
        await self.store.save()
        games_str = "、".join(GAMES[b]["name"] for b in roles)
        n = len(user["accounts"])
        msg = f"米游社账号 {bbs_uid}（{first_role.get('nickname', '')}），识别游戏：{games_str}"
        if n > 1:
            msg += f"；当前共 {n} 个账号，已切换到新账号（发送「米游社 切换 <序号>」可切换）"
        return True, msg, user["active_index"]

    # --------------------------------------------------------
    # 定时自动签到
    # --------------------------------------------------------

    async def _scheduler_loop(self):
        """定时任务主循环：每天配置时间执行全员自动签到。

        AstrBot WebUI 修改配置时不会打断已经开始的长时间 sleep，所以这里短轮询
        并重新读取 sign_time，确保改时间后无需重载插件也能及时生效。
        """
        last_run_key = ""
        last_logged_next = ""
        while True:
            try:
                now = datetime.now(BJT)
                hh, mm, hm = self._normalized_sign_time()
                target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                run_key = f"{now.date().isoformat()} {hm}"

                if now >= target:
                    if (
                        last_run_key != run_key
                        and (now - target).total_seconds() <= SCHEDULER_DUE_GRACE_SECONDS
                    ):
                        last_run_key = run_key
                        logger.info(f"到达每日自动签到时间 {hm}，开始执行")
                        await self._auto_sign_all()
                        last_logged_next = ""
                    target += timedelta(days=1)

                next_text = target.strftime("%Y-%m-%d %H:%M:%S")
                if next_text != last_logged_next:
                    last_logged_next = next_text
                    logger.info(f"米游社签到插件：下次每日自动签到时间 {next_text}（北京时间）")

                wait_seconds = max(1, min(SCHEDULER_POLL_SECONDS, (target - now).total_seconds()))
                await asyncio.sleep(wait_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时签到循环异常: {e}")
                await asyncio.sleep(60)

    def _normalized_sign_time(self) -> tuple[int, int, str]:
        hm = str(self.config.get("sign_time", "00:10")).strip()
        try:
            hh, mm = hm.split(":", 1)
            hour = int(hh)
            minute = int(mm)
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError
            return hour, minute, f"{hour:02d}:{minute:02d}"
        except Exception:
            logger.warning(f"sign_time 配置无效: {hm!r}，已使用默认 00:10")
            return 0, 10, "00:10"

    def _enabled_games(self) -> list:
        cfg = self.config.get("games") or {}
        if not isinstance(cfg, dict) or not cfg:
            return list(GAMES.keys())
        return [biz for biz, on in cfg.items() if on and biz in GAMES]

    async def _auto_sign_all(self):
        """为所有绑定用户的全部账号执行每日自动签到"""
        today = datetime.now(BJT).date().isoformat()
        enabled = self._enabled_games()
        async with self._sign_lock:
            for sender, user in list(self.store.data.items()):
                try:
                    if user.get("last_auto_sign_date") == today:
                        continue
                    accounts = user.get("accounts") or []
                    if not accounts:
                        continue
                    all_lines = []
                    for ai, acc in enumerate(accounts):
                        lines = []
                        for biz in enabled:
                            try:
                                lines.append(await self._sign_one(acc, biz))
                            except Exception as e:
                                lines.append(f"【{GAMES[biz]['name']}】❌ 出错：{e}")
                            await asyncio.sleep(0.8)  # 轻微限速，防风控
                        tag = ""
                        if len(accounts) > 1:
                            tag = f"📎 账号{ai + 1}（{acc.get('nickname', acc.get('bbs_uid', '?'))}）\n"
                        all_lines.append(tag + "\n".join(lines))
                    user["last_auto_sign_date"] = today
                    if self.config.get("push_result", True) and user.get("umo"):
                        await self._send(user["umo"], "🕐 每日自动签到结果\n" + "\n".join(all_lines))
                except Exception as e:
                    logger.error(f"自动签到失败 user={sender}: {e}")
            await self.store.save()

    # --------------------------------------------------------
    # 指令
    # --------------------------------------------------------

    @filter.command_group("米游社", alias={"mys"})
    def mihoyo(self):
        """米游社签到插件"""
        pass

    @mihoyo.command("帮助", priority=10)
    async def help_cmd(self, event: AstrMessageEvent):
        '''米游社签到帮助'''
        yield event.plain_result(
            "「米游社」每日签到插件使用说明\n"
            "——————————————\n"
            "📌 绑定方式（二选一）：\n"
            "1. 扫码登录：发送「米游社 扫码」\n"
            "2. Cookie 绑定：发送「米游社 绑定 <cookie>」\n"
            "   （浏览器登录 user.mihoyo.com 后 F12 → Console 输入 document.cookie 复制）\n"
            "——————————————\n"
            "📌 常用指令：\n"
            "· 签到 —— 一键签到当前账号的全部游戏（含全部角色）\n"
            "· 米游社 扫码 —— 扫码登录自动绑定\n"
            "· 米游社 绑定 <cookie> —— 绑定 Cookie\n"
            "· 米游社 签到 [游戏] —— 指定游戏签到（原神/崩铁/崩坏3/绝区零/崩坏2/未定）\n"
            "· 米游社 查询 [游戏] —— 查询本月签到状态（含全部角色）\n"
            "· 米游社 我的 —— 查看所有绑定账号\n"
            "· 米游社 切换 <序号> —— 多账号时切换默认账号\n"
            "· 米游社 解绑 [全部] —— 解绑当前账号/全部账号\n"
            "——————————————\n"
            "支持游戏：原神 / 崩坏：星穹铁道 / 崩坏3 / 绝区零 / 崩坏学园2 / 未定事件簿\n"
            "每日 {time} 自动签到（所有账号），结果自动推送~".format(
                time=str(self.config.get("sign_time", "00:10")))
        )
        event.stop_event()

    # ---- 快捷签到：直接发「签到」两个字 ----
    @filter.command("签到", alias={"打卡"}, priority=10)
    async def quick_sign(self, event: AstrMessageEvent):
        '''一键签到当前账号的全部游戏'''
        sender = event.get_sender_id()
        acc = self._active_account(sender)
        if not acc:
            yield event.plain_result("你还没有绑定米游社账号哦~ 发送「米游社 扫码」或「米游社 绑定 <cookie>」即可绑定")
            event.stop_event()
            return
        game = self._parse_game_from_msg(event.message_str, "签到")
        games = self._resolve_games(game)
        if not games:
            yield event.plain_result("未识别的游戏~ 发送「米游社 帮助」查看支持的游戏")
            event.stop_event()
            return
        lines = []
        async with self._sign_lock:
            for biz in games:
                try:
                    lines.append(await self._sign_one(acc, biz))
                except Exception as e:
                    lines.append(f"【{GAMES[biz]['name']}】❌ 出错：{e}")
            await self.store.save()
        prefix = self._account_prefix(sender)
        yield event.plain_result(prefix + "\n".join(lines))
        event.stop_event()

    @mihoyo.command("扫码", priority=10)
    async def qr_login(self, event: AstrMessageEvent):
        '''扫码登录绑定米游社账号'''
        sender = event.get_sender_id()
        task = self._qr_tasks.get(sender)
        if task and not task.done():
            yield event.plain_result("⏳ 已有正在等待扫码的二维码，请先完成或等待其过期~")
            event.stop_event()
            return

        device_id = self._get_user_device(sender)
        try:
            url, ticket = await self._create_qr(device_id)
        except Exception as e:
            yield event.plain_result(f"❌ 生成二维码失败：{e}")
            event.stop_event()
            return
        if not url or not ticket:
            yield event.plain_result("❌ 生成二维码失败：接口未返回有效数据")
            event.stop_event()
            return

        # 生成二维码图片
        import qrcode  # 延迟导入：qrcode 仅在扫码时使用，避免依赖缺失导致插件加载失败
        img_path = os.path.join(self.data_dir, f"qr_{sender}.png")
        try:
            qr = qrcode.QRCode(border=2, box_size=10)
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            img.save(img_path)
        except Exception as e:
            yield event.plain_result(f"❌ 生成二维码图片失败：{e}")
            event.stop_event()
            return

        timeout = max(30, int(self.config.get("qr_timeout", 120)))
        t = asyncio.create_task(self._poll_qr(ticket, device_id, sender,
                                               event.unified_msg_origin, timeout))
        self._qr_tasks[sender] = t

        yield event.chain_result([
            Comp.Image.fromFileSystem(img_path),
            Comp.Plain(f"请用米游社 App 扫描上方二维码登录（{timeout} 秒内有效），扫码确认后自动绑定~"),
        ])
        event.stop_event()

    @mihoyo.command("绑定", priority=10)
    async def bind(self, event: AstrMessageEvent):
        '''绑定米游社 Cookie'''
        text = event.message_str.strip()
        for prefix in ("/米游社", "米游社", "/mys", "mys"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        idx = text.find("绑定")
        if idx >= 0:
            text = text[idx + 2:].strip()
        if not text or len(text) < 10 or "=" not in text:
            yield event.plain_result(
                "用法：米游社 绑定 <cookie>\n"
                "获取方法：浏览器登录 user.mihoyo.com → F12 → Console 输入 document.cookie 回车 → 复制结果")
            event.stop_event()
            return
        sender = event.get_sender_id()
        ok, msg, _ = await self._validate_and_bind(sender, text, event.unified_msg_origin)
        if ok:
            yield event.plain_result(f"✅ 绑定成功！{msg}\n之后每天 {self.config.get('sign_time', '00:10')} 自动签到，也可以直接发送「签到」一键签到~")
        else:
            yield event.plain_result(f"❌ 绑定失败：{msg}")
        event.stop_event()

    @mihoyo.command("签到", priority=10)
    async def sign(self, event: AstrMessageEvent):
        '''手动签到当前账号，可指定游戏'''
        sender = event.get_sender_id()
        acc = self._active_account(sender)
        if not acc:
            yield event.plain_result("你还没有绑定米游社账号哦~ 发送「米游社 扫码」或「米游社 绑定 <cookie>」即可绑定")
            event.stop_event()
            return
        game = self._parse_game_from_msg(event.message_str, "签到")
        games = self._resolve_games(game)
        if not games:
            yield event.plain_result(f"未识别的游戏：{game}（支持：原神、崩铁、崩坏3、绝区零、崩坏2、未定）")
            event.stop_event()
            return
        lines = []
        async with self._sign_lock:
            for biz in games:
                try:
                    lines.append(await self._sign_one(acc, biz))
                except Exception as e:
                    lines.append(f"【{GAMES[biz]['name']}】❌ 出错：{e}")
            await self.store.save()
        prefix = self._account_prefix(sender)
        yield event.plain_result(prefix + "\n".join(lines))
        event.stop_event()

    @mihoyo.command("查询", priority=10)
    async def query(self, event: AstrMessageEvent):
        '''查询当前账号本月签到状态（含全部角色）'''
        sender = event.get_sender_id()
        acc = self._active_account(sender)
        if not acc:
            yield event.plain_result("你还没有绑定米游社账号哦~ 发送「米游社 扫码」或「米游社 绑定 <cookie>」即可绑定")
            event.stop_event()
            return
        game = self._parse_game_from_msg(event.message_str, "查询")
        games = self._resolve_games(game)
        if not games:
            yield event.plain_result(f"未识别的游戏：{game}")
            event.stop_event()
            return
        lines = ["📊 本月签到状态："]
        prefix = self._account_prefix(sender).strip()
        if prefix:
            lines.insert(0, prefix)
        for biz in games:
            g = GAMES[biz]
            roles_map = acc.get("roles") or {}
            role_list = _as_role_list(roles_map.get(biz))
            try:
                if not role_list:
                    role_list, err = await self._get_roles(acc.get("cookie", ""), biz)
                    if not role_list:
                        lines.append(f"· {g['name']}：{err}")
                        continue
                    roles_map[biz] = role_list
                _, info_url, _ = self._sign_urls(biz)
                for role in role_list:
                    region = role.get("region") or ""
                    game_uid = str(role.get("game_uid") or "")
                    rname = role.get("region_name") or region
                    j = await self._request(
                        "GET", info_url, cookie=acc.get("cookie", ""),
                        params={"lang": "zh-cn", "act_id": g["act_id"],
                                "region": region, "uid": game_uid},
                        game_biz=biz)
                    if j.get("retcode") == 0:
                        info = j.get("data") or {}
                        total = info.get("total_sign_day") or 0
                        if info.get("first_bind"):
                            lines.append(f"· {g['name']}（{rname} · {game_uid}）：首次绑定，请先在米游社手动签到一次")
                            continue
                        is_sign = _is_today_signed(info)
                        lines.append(
                            f"· {g['name']}（{rname} · {game_uid}）："
                            f"本月已签 {total} 天，今日{'已签 ✅' if is_sign else '未签 ❌'}")
                    elif j.get("retcode") == RETCODE_CAPTCHA:
                        lines.append(f"· {g['name']}（{rname} · {game_uid}）：触发验证码（1034），请稍后手动查询")
                    else:
                        lines.append(f"· {g['name']}（{rname} · {game_uid}）：查询失败（{j.get('message', j.get('retcode'))}）")
            except Exception as e:
                lines.append(f"· {g['name']}：出错 {e}")
        await self.store.save()
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    @mihoyo.command("我的", priority=10)
    async def me(self, event: AstrMessageEvent):
        '''查看所有绑定账号'''
        sender = event.get_sender_id()
        user = self.store.data.get(sender)
        accounts = (user or {}).get("accounts") or []
        if not accounts:
            yield event.plain_result("你还没有绑定米游社账号哦~")
            event.stop_event()
            return
        lines = [f"📌 已绑定的米游社账号（共 {len(accounts)} 个）"]
        for i, acc in enumerate(accounts):
            bt = datetime.fromtimestamp(acc.get("bound_at", 0)).strftime("%Y-%m-%d %H:%M")
            roles = acc.get("roles") or {}
            parts = []
            for b, r in roles.items():
                rl = _as_role_list(r)
                uids = "、".join(x.get("game_uid", "?") for x in rl) or "?"
                parts.append(f"{GAMES[b]['name']}（{uids}）")
            games_str = "、".join(parts) or "（无角色信息，发送「签到」可刷新）"
            active = " ← 当前" if i == (user or {}).get("active_index", 0) else ""
            lines.append(
                f"\n{i + 1}. UID：{acc.get('bbs_uid', '?')}（{acc.get('nickname', '?')}）{active}\n"
                f"   绑定时间：{bt}\n   游戏：{games_str}")
        lines.append("\n发送「米游社 切换 <序号>」切换默认账号；「米游社 解绑」解绑当前账号")
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    @mihoyo.command("切换", priority=10)
    async def switch(self, event: AstrMessageEvent):
        '''多账号切换默认账号'''
        sender = event.get_sender_id()
        user = self.store.data.get(sender)
        accounts = (user or {}).get("accounts") or []
        if not accounts:
            yield event.plain_result("你还没有绑定米游社账号哦~")
            event.stop_event()
            return
        arg = self._parse_game_from_msg(event.message_str, "切换")
        if not arg:
            lst = "\n".join(
                f"{i + 1}. {a.get('bbs_uid', '?')}（{a.get('nickname', '?')}）"
                f"{' ← 当前' if i == (user or {}).get('active_index', 0) else ''}"
                for i, a in enumerate(accounts))
            yield event.plain_result(f"当前共 {len(accounts)} 个账号：\n{lst}\n发送「米游社 切换 <序号>」切换默认账号")
            event.stop_event()
            return
        try:
            idx = int(arg) - 1
        except ValueError:
            idx = next((i for i, a in enumerate(accounts)
                        if arg in (a.get("nickname") or "") or arg in (a.get("bbs_uid") or "")), -1)
        if idx < 0 or idx >= len(accounts):
            yield event.plain_result(f"没有找到「{arg}」，发送「米游社 切换」查看账号列表")
            event.stop_event()
            return
        user["active_index"] = idx
        await self.store.save()
        acc = accounts[idx]
        yield event.plain_result(f"已切换到账号 {idx + 1}：{acc.get('bbs_uid', '?')}（{acc.get('nickname', '?')}）")
        event.stop_event()

    @mihoyo.command("解绑", priority=10)
    async def unbind(self, event: AstrMessageEvent):
        '''解绑当前账号（加「全部」解绑所有）'''
        sender = event.get_sender_id()
        user = self.store.data.get(sender)
        accounts = (user or {}).get("accounts") or []
        if not accounts:
            yield event.plain_result("你还没有绑定米游社账号哦~")
            event.stop_event()
            return
        text = self._parse_game_from_msg(event.message_str, "解绑")
        if text in ("全部", "all", "所有"):
            del self.store.data[sender]
            await self.store.save()
            yield event.plain_result("已解绑全部账号。期待你随时回来绑定~")
            event.stop_event()
            return
        idx = (user or {}).get("active_index", 0)
        acc = accounts[idx]
        nickname = acc.get("nickname", acc.get("bbs_uid", "?"))
        accounts.pop(idx)
        if not accounts:
            del self.store.data[sender]
            remain = ""
        else:
            if idx >= len(accounts):
                user["active_index"] = 0
            remain = "发送「米游社 我的」可查看剩余账号~"
        await self.store.save()
        yield event.plain_result(f"已解绑账号 {idx + 1}（{nickname}）。{remain}")
        event.stop_event()

    # 管理员：手动触发全员自动签到
    @filter.permission_type(filter.PermissionType.ADMIN)
    @mihoyo.command("全部签到", priority=10)
    async def sign_all(self, event: AstrMessageEvent):
        '''管理员：立即为所有绑定用户的全部账号签到'''
        yield event.plain_result("⏳ 开始为所有绑定用户签到，请稍候…")
        try:
            await self._auto_sign_all()
            yield event.plain_result("✅ 全员签到完成")
        except Exception as e:
            yield event.plain_result(f"❌ 全员签到出错：{e}")
        event.stop_event()

    # --------------------------------------------------------
    # 生命周期
    # --------------------------------------------------------

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        """AstrBot 加载完成后启动定时任务"""
        if self._sched_task is None or self._sched_task.done():
            self._sched_task = asyncio.create_task(self._scheduler_loop())
            logger.info("米游社签到插件：每日自动签到任务已启动")

    async def terminate(self):
        """插件卸载 / 停用时清理任务"""
        if self._sched_task:
            self._sched_task.cancel()
        for t in list(self._qr_tasks.values()):
            t.cancel()
        self._qr_tasks.clear()

    # --------------------------------------------------------
    # 辅助
    # --------------------------------------------------------

    def _account_prefix(self, sender: str) -> str:
        """多账号时在签到结果前标注当前账号"""
        user = self.store.data.get(sender)
        accounts = (user or {}).get("accounts") or []
        if len(accounts) <= 1:
            return ""
        idx = (user or {}).get("active_index", 0)
        acc = accounts[idx] if 0 <= idx < len(accounts) else accounts[0]
        return f"📎 当前账号 {idx + 1}/{len(accounts)}（{acc.get('nickname', acc.get('bbs_uid', '?'))}）\n"

    def _parse_game_from_msg(self, msg: str, cmd: str) -> str:
        """从消息文本中解析指令后的游戏名参数"""
        msg = (msg or "").strip()
        for prefix in ("/米游社", "米游社", "/mys", "mys", "/签到", "签到", "/打卡", "打卡"):
            if msg.startswith(prefix):
                msg = msg[len(prefix):].strip()
                break
        idx = msg.find(cmd)
        if idx >= 0:
            msg = msg[idx + len(cmd):].strip()
        return msg

    def _resolve_games(self, arg: str) -> list:
        """解析游戏参数：空 -> 全部已启用游戏；否则返回单个"""
        if not arg or not arg.strip():
            return self._enabled_games()
        biz = GAME_ALIASES.get(arg.strip().lower())
        if biz:
            return [biz]
        return []
