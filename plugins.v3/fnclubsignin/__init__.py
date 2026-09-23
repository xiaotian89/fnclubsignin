# -*- coding: utf-8 -*-
"""飞牛论坛签到插件 - MoviePilot V3

自动登录飞牛私有云论坛(club.fnnas.com)并完成"天天打卡"签到，获取飞牛币奖励。

功能：
- 每日定时自动签到（可配置时间 + 随机错峰）
- 两种登录方式：Cookie 直签（推荐） / 账号密码模拟登录
- 人类化浏览：打卡前模拟真实用户先逛首页/板块，随机间隔再打卡
- 随机 User-Agent 池轮换，降低脚本特征
- 解析打卡结果（成功 / 今日已打卡 / 需验证码）
- 签到结果推送到 MoviePilot 通知渠道
- 签到历史记录（最近30次）

注意：飞牛社区规则明确"请勿使用插件打卡，异常打卡经核实，将会扣除飞牛币"。
使用本插件即视为接受该风险。本插件通过随机错峰、随机UA、模拟浏览等方式
尽可能贴近真人行为，但无法完全消除被识别为自动化的风险。
"""
from __future__ import annotations

import asyncio
import random
import re
import time
from datetime import datetime
from typing import Any, Optional

import httpx2
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.schemas.types import EventType


class FnClubSignin(_PluginBase):
    """飞牛论坛(club.fnnas.com)每日自动签到。"""

    plugin_name = "飞牛论坛签到"
    plugin_desc = "自动登录飞牛私有云论坛(club.fnnas.com)完成天天打卡，获取飞牛币。"
    plugin_icon = "https://club.fnnas.com/favicon.ico"
    plugin_version = "1.4.0"
    plugin_author = "xiaotian"
    author_url = "https://club.fnnas.com"
    plugin_config_prefix = "fnnassignin_"
    plugin_order = 20
    auth_level = 1

    # 站点常量
    _base_url = "https://club.fnnas.com"
    _sign_page = "/plugin.php?id=zqlj_sign"
    _login_api = (
        "/member.php?mod=logging&action=login&loginsubmit=yes"
        "&infloat=yes&lssubmit=yes&inajax=1"
    )
    # 随机 UA 池：模拟不同浏览器的真实 UA，避免固定 UA 的脚本特征
    _user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ]
    # 人类化浏览的页面池（打卡前"顺路逛"的页面）
    _browse_pages = [
        "/",
        "/forum.php",
        "/forum-46-1.html",
        "/forum.php?mod=forumdisplay&fid=46",
    ]

    _enabled = False
    _username = ""
    _password = ""
    _cookie = ""
    _cron = "0 8 * * *"
    _notify = True
    _delay_seconds = 1800
    _humanize = True

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """读取配置并建立签到所需状态。"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._username = str(config.get("username") or "").strip()
        self._password = str(config.get("password") or "").strip()
        self._cookie = str(config.get("cookie") or "").strip()
        self._cron = str(config.get("cron") or "0 8 * * *").strip()
        self._notify = bool(config.get("notify", True))
        # 随机错峰：0-7200 秒随机延迟，避免每天准点打卡的脚本特征
        self._delay_seconds = int(config.get("delay_seconds") or 1800)
        if self._delay_seconds > 7200:
            self._delay_seconds = 7200
        self._humanize = bool(config.get("humanize", True))

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """注册远程命令：/fnclub_sign 手动执行一次签到。"""
        return [
            {
                "cmd": "/fnclub_sign",
                "event": EventType.PluginAction,
                "desc": "手动执行飞牛论坛签到",
                "category": "插件命令",
                "data": {"action": "fnclub_sign"},
            }
        ]

    def get_api(self) -> list[dict[str, Any]]:
        """注册后端 API：查询签到历史与立即签到。"""
        return [
            {
                "path": "/history",
                "endpoint": self.get_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询飞牛论坛签到历史",
            },
            {
                "path": "/sign_now",
                "endpoint": self.sign_now,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "立即执行一次飞牛论坛签到",
            },
        ]

    def get_service(self) -> list[dict]:
        """注册每日定时签到任务。"""
        if not self.get_state():
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except Exception as err:
            logger.warning(f"飞牛签到：定时表达式无效({err})，使用默认 08:00")
            trigger = CronTrigger.from_crontab("0 8 * * *")
        return [
            {
                "id": "FnClubSignin.Daily",
                "name": "飞牛论坛每日签到",
                "trigger": trigger,
                "func": self.schedule_sign,
                "kwargs": {},
            }
        ]

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """返回配置页面：V2 风格分区卡片。"""
        glass = (
            "background-color: rgba(var(--v-theme-surface), 0.72); "
            "color: rgb(var(--v-theme-on-surface)); "
            "border: 1px solid rgba(var(--v-theme-on-surface), 0.10); "
            "border-radius: 10px;"
        )

        def icon_tile(icon: str, color: str, size: int = 34) -> dict:
            return {
                "component": "div",
                "props": {
                    "class": "d-flex align-center justify-center",
                    "style": (
                        f"width: {size}px; height: {size}px; border-radius: 10px; "
                        f"background: color-mix(in srgb, {color} 14%, transparent);"
                    ),
                },
                "content": [
                    {"component": "VIcon", "props": {"size": int(size * 0.56), "color": color}, "text": icon},
                ],
            }

        def section(title: str, icon: str, color: str, content: list) -> dict:
            return {
                "component": "div",
                "props": {"class": "mb-3", "style": glass},
                "content": [
                    {
                        "component": "div",
                        "props": {
                            "class": "d-flex align-center ga-2",
                            "style": "padding: 10px 14px 6px 14px;",
                        },
                        "content": [
                            icon_tile(icon, color),
                            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold"}, "text": title},
                        ],
                    },
                    {
                        "component": "VRow",
                        "props": {"class": "pa-2", "dense": True},
                        "content": content,
                    },
                ],
            }

        def field(model: str, label: str, placeholder: str = "", type_: str = "text", md: int = 12) -> dict:
            props: dict[str, Any] = {
                "model": model, "label": label, "placeholder": placeholder,
                "variant": "outlined", "density": "comfortable", "hide-details": True,
            }
            if type_ != "text":
                props["type"] = type_
            return {
                "component": "VCol",
                "props": {"cols": 12, "md": md},
                "content": [{"component": "VTextField", "props": props}],
            }

        def switch(model: str, label: str, color: str, hint: str = "", md: int = 6) -> dict:
            props: dict[str, Any] = {"model": model, "label": label, "color": color, "hide-details": True}
            if hint:
                props["hint"] = hint
                props["persistent-hint"] = True
            return {
                "component": "VCol",
                "props": {"cols": 12, "md": md},
                "content": [{"component": "VSwitch", "props": props}],
            }

        return [
            {
                "component": "VForm",
                "content": [
                    section("启用", "mdi-power", "#4CAF50", [switch("enabled", "启用插件", "#4CAF50", "开启后按定时任务自动打卡", 12)]),
                    section(
                        "登录方式",
                        "mdi-account-key",
                        "#2196F3",
                        [
                            field("username", "飞牛论坛用户名（可选）", "配置了Cookie时可留空", md=6),
                            field("password", "飞牛论坛密码（可选）", "配置了Cookie时可留空", type_="password", md=6),
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "cookie",
                                            "label": "论坛登录Cookie（推荐）",
                                            "placeholder": "浏览器登录 club.fnnas.com 后复制的 Cookie 字符串，优先使用",
                                            "variant": "outlined",
                                            "density": "comfortable",
                                            "auto-grow": True,
                                            "rows": 3,
                                        },
                                    }
                                ],
                            },
                        ],
                    ),
                    section(
                        "签到设置",
                        "mdi-clock-outline",
                        "#FF9800",
                        [
                            field("cron", "签到时间(Cron)", "默认 0 8 * * *（每天08:00）", md=6),
                            field("delay_seconds", "随机错峰秒数(0-7200)", "默认1800：定时触发后随机延迟0-30分钟", md=6),
                            switch("humanize", "人类化浏览(推荐)", "#4CAF50", "打卡前模拟浏览首页/板块，降低脚本特征"),
                            switch("notify", "签到结果通知", "#2196F3"),
                        ],
                    ),
                ],
            }
        ], {
            "enabled": self._enabled,
            "username": self._username,
            "password": self._password,
            "cookie": self._cookie,
            "cron": self._cron,
            "delay_seconds": self._delay_seconds,
            "humanize": self._humanize,
            "notify": self._notify,
        }

    def get_page(self) -> list[dict]:
        """返回签到状态详情页：V2 风格状态卡 + 统计瓦片 + 历史列表。"""
        history = self.get_history_sync()
        total = len(history)
        success = sum(1 for h in history if isinstance(h, dict) and h.get("result") == "成功")
        last = history[-1] if history and isinstance(history[-1], dict) else {}
        last_time = str(last.get("time") or "—")[:16]
        last_result = last.get("result") if last else "暂无"
        last_detail = last.get("detail") if last else "保存配置后等待定时任务或点击立即签到"

        mode = "Cookie 直签" if self._cookie else ("账号密码" if self._username else "未配置")
        human = "开启" if self._humanize else "关闭"
        glass = (
            "background-color: rgba(var(--v-theme-surface), 0.72); "
            "color: rgb(var(--v-theme-on-surface)); "
            "border: 1px solid rgba(var(--v-theme-on-surface), 0.10); "
            "border-radius: 10px;"
        )

        def stat(icon: str, label: str, value: str, color: str) -> dict:
            return {
                "component": "VCol",
                "props": {"cols": 6, "md": 3, "class": "pa-1"},
                "content": [
                    {
                        "component": "div",
                        "props": {
                            "class": "d-flex flex-column align-center justify-center pa-2",
                            "style": glass,
                        },
                        "content": [
                            {
                                "component": "div",
                                "props": {"class": "d-flex align-center justify-center ga-1"},
                                "content": [
                                    {"component": "VIcon", "props": {"size": 15, "color": color}, "text": icon},
                                    {"component": "span", "props": {"class": "text-subtitle-2 font-weight-bold"}, "text": str(value)},
                                ],
                            },
                            {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-1"}, "text": label},
                        ],
                    }
                ],
            }

        # 历史记录卡片
        history_rows = []
        for item in history[-10:]:
            ok = item.get("result") == "成功"
            history_rows.append(
                {
                    "component": "div",
                    "props": {"class": "d-flex align-center ga-3", "style": f"{glass} padding: 8px 12px; margin-bottom: 6px;"},
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "d-flex align-center justify-center flex-shrink-0",
                                      "style": f"width: 30px; height: 30px; border-radius: 50%; background: color-mix(in srgb, {'#4CAF50' if ok else '#F44336'} 14%, transparent);"},
                            "content": [
                                {"component": "VIcon", "props": {"size": 16, "color": "#4CAF50" if ok else "#F44336"},
                                 "text": "mdi-check-circle" if ok else "mdi-close-circle"},
                            ],
                        },
                        {
                            "component": "div",
                            "props": {"class": "flex-grow-1", "style": "min-width: 0;"},
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"class": "text-body-2"},
                                    "content": [
                                        {"component": "span", "props": {"class": "font-weight-bold"}, "text": str(item.get("time") or "")[:16]},
                                        {"component": "span", "props": {"class": "text-medium-emphasis"}, "text": f"  {item.get('message') or ''}"},
                                    ],
                                },
                                {"component": "div", "props": {"class": "text-caption text-medium-emphasis", "style": "overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"},
                                 "text": str(item.get("detail") or "")},
                            ],
                        },
                    ],
                }
            )
        if not history_rows:
            history_rows = [
                {
                    "component": "div",
                    "props": {"class": "text-caption text-medium-emphasis pa-4", "style": glass},
                    "text": "暂无签到记录，保存配置后等待定时任务或点击立即签到。",
                }
            ]

        return [
            # 顶部状态卡
            {
                "component": "div",
                "props": {"class": "d-flex align-center ga-3 mb-2", "style": f"{glass} padding: 12px 16px;"},
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "d-flex align-center justify-center",
                                  "style": "width: 42px; height: 42px; border-radius: 12px; background: color-mix(in srgb, #2196F3 14%, transparent);"},
                        "content": [{"component": "VIcon", "props": {"size": 22, "color": "#2196F3"}, "text": "mdi-calendar-check"}],
                    },
                    {
                        "component": "div",
                        "props": {"class": "flex-grow-1"},
                        "content": [
                            {"component": "div", "props": {"class": "text-body-2 font-weight-bold"}, "text": "飞牛论坛天天打卡"},
                            {
                                "component": "div",
                                "props": {"class": "text-caption text-medium-emphasis mt-1"},
                                "text": (
                                    f"登录：{mode} ｜ 定时：{self._cron} ｜ 错峰：{self._delay_seconds}s ｜ 人类化：{human}"
                                ),
                            },
                        ],
                    },
                ],
            },
            # 统计瓦片
            {
                "component": "VRow",
                "props": {"dense": True, "class": "mb-1"},
                "content": [
                    stat("mdi-check-circle", "最近结果", last_result, "#4CAF50" if last_result == "成功" else "#FF9800"),
                    stat("mdi-clock-outline", "最近时间", last_time, "#2196F3"),
                    stat("mdi-history", "签到次数", total, "#9C27B0"),
                    stat("mdi-trophy", "成功次数", success, "#FF9800"),
                ],
            },
            # 最近一条详情
            {
                "component": "div",
                "props": {"class": "text-caption text-medium-emphasis mb-2", "style": f"{glass} padding: 8px 12px;"},
                "text": f"最近详情：{last_detail}",
            },
            # 历史列表
            {
                "component": "div",
                "props": {"class": "mt-1"},
                "content": [
                    {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mb-1"}, "text": "签到历史（最近10条）"},
                    {"component": "div", "content": history_rows},
                ],
            },
        ]

    def stop_service(self) -> None:
        """停用插件时释放资源。"""
        self._enabled = False

    # ---------- 事件处理 ----------

    @eventmanager.register(EventType.PluginAction)
    def _on_plugin_action(self, event: Event) -> None:
        """处理远程命令事件。"""
        event_data = getattr(event, "event_data", None) or {}
        if event_data.get("action") != "fnclub_sign":
            return
        self._safe_run(self._run_sign())

    # ---------- API ----------

    async def sign_now(self) -> dict:
        """API：立即执行一次签到（跳过随机错峰，快速返回结果）。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        result = await self._run_sign(skip_delay=True)
        return result

    async def get_history(self) -> dict:
        """API：返回签到历史。"""
        return {"success": True, "data": self.get_history_sync()}

    # ---------- 核心签到逻辑 ----------

    @staticmethod
    def _safe_run(coro) -> None:
        """安全运行异步协程：优先复用现有事件循环，否则新建。"""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
            return
        except RuntimeError:
            asyncio.run(coro)

    def schedule_sign(self) -> None:
        """定时任务入口（在调度线程中执行，保留随机错峰）。"""
        self._safe_run(self._run_sign(skip_delay=False))

    async def _run_sign(self, skip_delay: bool = False) -> dict:
        """执行一次完整签到流程，返回结构化结果。

        定时任务触发时保留随机错峰（防脚本特征）；手动触发(skip_delay=True)
        跳过错峰立即执行，方便即时验证。
        """
        result = {"success": False, "message": "", "detail": ""}
        try:
            # 随机错峰：避免每天同一秒打卡的脚本特征（仅定时任务）
            if self._delay_seconds > 0 and not skip_delay:
                wait = random.randint(0, self._delay_seconds)
                if wait > 0:
                    logger.info(f"飞牛签到：随机错峰延迟 {wait} 秒")
                    await asyncio.sleep(wait)

            outcome = await self._do_sign()
            result.update(outcome)
        except Exception as err:
            logger.error(f"飞牛论坛签到异常：{err}", exc_info=True)
            result["message"] = "签到异常"
            result["detail"] = str(err)

        # 记录历史
        self._record(result)
        # 通知
        if self._notify:
            try:
                self.post_message(
                    title=f"飞牛论坛签到{'成功' if result['success'] else '失败'}",
                    text=(
                        f"账号：{self._username or 'Cookie模式'}\n"
                        f"结果：{result['message']}\n"
                        f"详情：{result['detail']}"
                    ),
                )
            except Exception as err:
                logger.warning(f"飞牛签到通知发送失败：{err}")
        return result

    async def _do_sign(self) -> dict:
        """核心签到：Cookie 直签优先，失效自动降级账号密码登录。"""
        # 模式一：Cookie 直签（推荐，无需账号密码）
        if self._cookie:
            result = await self._do_sign_cookie()
            # Cookie 失效且配置了账号密码 → 自动降级为账号密码登录
            if (not result.get("success")) and result.get("message") == "登录态失效" \
                    and self._username and self._password:
                logger.info("飞牛签到：Cookie 失效，自动降级账号密码登录")
                return await self._do_sign_login(fallback_from="Cookie 失效")
            return result
        # 模式二：账号密码登录
        if not self._username or not self._password:
            return {"success": False, "message": "未配置 Cookie 或账号密码", "detail": "请在插件配置中填写 Cookie 或账号密码"}
        return await self._do_sign_login(fallback_from="")

    async def _do_sign_login(self, fallback_from: str = "") -> dict:
        """账号密码登录签到：登录(带验证) → 打卡。登录成功自动保存新 Cookie 供下次直签。"""
        async with httpx2.AsyncClient(
            timeout=httpx2.Timeout(30.0),
            follow_redirects=True,
            headers=self._base_headers(),
        ) as client:
            # 1. 打开打卡页，获取 formhash 与 sign
            page_html = await self._fetch(client, self._base_url + self._sign_page)
            if not page_html:
                return {"success": False, "message": "无法访问打卡页", "detail": ""}

            formhash = self._extract_formhash(page_html)
            if not formhash:
                return {"success": False, "message": "页面未返回 formhash", "detail": ""}

            # 2. 模拟登录（内置登录态二次验证）
            login_ok, login_detail = await self._login(client, formhash)
            if not login_ok:
                prefix = "（Cookie 失效后降级登录失败）" if fallback_from else ""
                return {"success": False, "message": "登录失败" + prefix, "detail": login_detail}

            # 3. 登录成功 → 保存新 Cookie（供下次 Cookie 直签）
            try:
                new_cookie = self._dump_cookies(client)
                if new_cookie:
                    self._cookie = new_cookie
                    self.update_config({"cookie": new_cookie})
                    logger.info("飞牛签到：登录成功，已自动更新 Cookie")
            except Exception as err:
                logger.warning(f"飞牛签到：登录后 Cookie 保存失败({err})")

            # 4. 登录后重新获取打卡页（拿新的 sign）
            page_html2 = await self._fetch(client, self._base_url + self._sign_page)
            if not page_html2:
                return {"success": False, "message": "登录后无法访问打卡页", "detail": ""}

            sign2 = self._extract_sign(page_html2)
            # 若页面已显示今日已打卡
            if "今日已打卡" in page_html2:
                return {"success": True, "message": "今日已打卡", "detail": "无需重复签到"}

            if not sign2:
                return {"success": False, "message": "未获取到打卡入口", "detail": "可能已打卡或需要验证码"}

            # 5. 执行打卡
            sign_url = self._base_url + f"/plugin.php?id=zqlj_sign&sign={sign2}"
            resp_html = await self._fetch(client, sign_url)
            if not resp_html:
                return {"success": False, "message": "打卡请求失败", "detail": ""}

            return self._parse_sign_result(resp_html)

    async def _do_sign_cookie(self) -> dict:
        """Cookie 直签模式：人类化浏览 → 访问打卡页 → 判定状态 → 执行打卡。"""
        async with httpx2.AsyncClient(
            timeout=httpx2.Timeout(30.0),
            follow_redirects=True,
            headers=self._base_headers(cookie=self._cookie),
        ) as client:
            # 0. 人类化浏览：先"顺路逛"几个页面，再进打卡页（降低直达打卡页的脚本特征）
            if self._humanize:
                await self._humanize_walk(client)

            # 1. 访问打卡页
            page_html = await self._fetch(client, self._base_url + self._sign_page)
            if not page_html:
                return {"success": False, "message": "无法访问打卡页", "detail": ""}

            # 登录态判定：Discuz 登录后页面含"退出"链接
            if "退出登录" not in page_html and ">退出<" not in page_html and "退出" not in page_html:
                return {"success": False, "message": "登录态失效", "detail": "Cookie 已过期，请重新登录论坛并更新 Cookie"}

            # 已打卡
            if "今日已打卡" in page_html:
                return {"success": True, "message": "今日已打卡", "detail": "无需重复签到"}

            # 提取打卡入口
            sign = self._extract_sign(page_html)
            if not sign:
                return {"success": False, "message": "未获取到打卡入口", "detail": "可能已打卡或需要验证码"}

            # 打卡前短暂停留（模拟阅读页面）
            await asyncio.sleep(random.uniform(0.8, 3.0))

            # 执行打卡（带 Referer 模拟从打卡页点击）
            sign_url = self._base_url + f"/plugin.php?id=zqlj_sign&sign={sign}"
            resp_html = await self._fetch(
                client, sign_url,
                referer=self._base_url + self._sign_page,
            )
            if not resp_html:
                return {"success": False, "message": "打卡请求失败", "detail": ""}

            return self._parse_sign_result(resp_html)

    async def _humanize_walk(self, client: httpx2.AsyncClient) -> None:
        """模拟人类浏览：随机逛 1-2 个页面，每次间隔随机，最后回打卡页。"""
        try:
            # 先访问首页（人类进入论坛的第一站）
            await self._fetch(client, self._base_url + "/")
            await asyncio.sleep(random.uniform(1.2, 3.5))
            # 随机逛 1-2 个板块/页面
            walk_count = random.randint(1, 2)
            visited = set()
            for _ in range(walk_count):
                page = random.choice(self._browse_pages)
                if page in visited:
                    continue
                visited.add(page)
                await self._fetch(client, self._base_url + page)
                await asyncio.sleep(random.uniform(2.0, 5.5))
        except Exception as err:
            logger.warning(f"飞牛签到：人类化浏览跳过({err})")

    async def _login(self, client: httpx2.AsyncClient, formhash: str) -> tuple[bool, str]:
        """Discuz 标准登录。返回 (是否成功, 失败原因)。成功后额外验证登录态，防止假成功。"""
        data = {
            "formhash": formhash,
            "username": self._username,
            "password": self._password,
            "cookietime": "2592000",
            "questionid": "0",
            "answer": "",
        }
        try:
            resp = await client.post(
                self._base_url + self._login_api,
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": self._base_url + self._sign_page,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            text = resp.text or ""
            low = text.lower()

            # 1) Discuz 返回的登录失败消息（含错误原因）
            if "succeed" not in low and ("error" in low or "失败" in text or "错误" in text):
                if "密码" in text or "password" in low:
                    return False, "用户名或密码错误"
                if "验证" in text or "captcha" in low or "seccode" in low:
                    return False, "触发登录验证码，请稍后重试或手动登录一次"
                if "不存在" in text or ("用户" in text and "不存在" in text):
                    return False, "账号不存在"
                if "频繁" in text or "受限" in text:
                    return False, "登录过于频繁或账号受限，请稍后重试"
                return False, (text.strip()[:150] or "登录被拒绝")

            # 2) 成功标志：ajax succeed / 欢迎 / 跳转
            if "succeed" in low or "欢迎" in text or ("location.href" in text and "logging" not in low):
                ok = await self._verify_login(client)
                return (True, "登录成功") if ok else (False, "登录态校验未通过（疑似验证码拦截）")

            # 3) 备用：cookie 判定（登录后设置 discuz_/uc_）
            for cookie in client.cookies.jar:
                if cookie.name.startswith(("discuz_", "uc_")):
                    ok = await self._verify_login(client)
                    return (True, "登录成功") if ok else (False, "登录态校验未通过（疑似验证码拦截）")

            logger.warning(f"飞牛签到登录响应未识别：{text[:200]}")
            return False, "登录响应无法识别，请检查账号配置"
        except Exception as err:
            logger.error(f"飞牛签到登录请求异常：{err}")
            return False, f"登录请求异常：{err}"

    async def _verify_login(self, client: httpx2.AsyncClient) -> bool:
        """登录后二次验证：访问首页，确认 Discuz 登录态（页面含"退出"链接）。"""
        try:
            html = await self._fetch(client, self._base_url + "/")
            if not html:
                return False
            if "退出登录" in html or ">退出<" in html or "退出" in html:
                return True
            # 兜底：discuz_auth / uc_auth 会话 cookie
            for cookie in client.cookies.jar:
                if cookie.value and cookie.name.lower().startswith(("discuz_", "uc_")) and "auth" in cookie.name.lower():
                    return True
            return False
        except Exception as err:
            logger.warning(f"飞牛签到登录态校验异常：{err}")
            return False

    def _dump_cookies(self, client: httpx2.AsyncClient) -> str:
        """把客户端 Cookie 序列化为请求头字符串（Discuz 会话）。"""
        try:
            parts = []
            for cookie in client.cookies.jar:
                if cookie.value:
                    parts.append(f"{cookie.name}={cookie.value}")
            return "; ".join(parts)
        except Exception:
            return ""

    def _base_headers(self, cookie: str = "") -> dict:
        """构造基础请求头：随机 UA + 可选携带 Cookie。"""
        headers = {
            "User-Agent": random.choice(self._user_agents),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def _fetch(self, client: httpx2.AsyncClient, url: str, referer: str = "") -> str:
        """GET 请求并返回页面文本，失败返回空字符串。"""
        try:
            kwargs: dict[str, Any] = {}
            if referer:
                kwargs["headers"] = {"Referer": referer}
            resp = await client.get(url, **kwargs)
            if resp.status_code != 200:
                logger.warning(f"飞牛签到 HTTP {resp.status_code}: {url}")
                return ""
            return resp.text or ""
        except Exception as err:
            logger.warning(f"飞牛签到请求失败 {url}: {err}")
            return ""

    @staticmethod
    def _extract_formhash(html: str) -> str:
        """从页面提取 formhash。"""
        m = re.search(r'name="formhash"\s+value="([a-f0-9]+)"', html)
        return m.group(1) if m else ""

    @staticmethod
    def _extract_sign(html: str) -> str:
        """从页面提取打卡 sign（点击打卡链接）。"""
        m = re.search(r'plugin\.php\?id=zqlj_sign&sign=([a-f0-9]+)"[^>]*class="btna"[^>]*>点击打卡', html)
        if not m:
            m = re.search(r'href="plugin\.php\?id=zqlj_sign&sign=([a-f0-9]+)"[^>]*>点击打卡', html)
        if not m:
            m = re.search(r'plugin\.php\?id=zqlj_sign&sign=([a-f0-9]+)', html)
        return m.group(1) if m else ""

    @staticmethod
    def _parse_sign_result(html: str) -> dict:
        """解析打卡响应。"""
        # 成功标志
        if "打卡成功" in html or "签到成功" in html:
            return {"success": True, "message": "打卡成功", "detail": "飞牛币已入账"}
        if "今日已打卡" in html:
            return {"success": True, "message": "今日已打卡", "detail": "无需重复签到"}
        # 验证码/滑块
        if "slide" in html.lower() or "verify" in html.lower() or "验证" in html:
            return {"success": False, "message": "需要滑块验证", "detail": "今日打卡触发真人验证，请手动打卡一次"}
        # 未登录
        if "login" in html.lower() and ("账号" in html or "密码" in html):
            return {"success": False, "message": "打卡需登录", "detail": "登录态失效，请检查账号配置"}
        # 其他
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return {"success": False, "message": "打卡结果未知", "detail": text[:120]}

    # ---------- 数据记录 ----------

    def _record(self, result: dict) -> None:
        """记录签到历史（保留最近30条）。"""
        try:
            history = self.get_history_sync()
            history.append(
                {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "result": "成功" if result["success"] else "失败",
                    "message": result["message"],
                    "detail": result["detail"],
                }
            )
            self.save_data("history", history[-30:])
        except Exception as err:
            logger.warning(f"飞牛签到记录历史失败：{err}")

    def get_history_sync(self) -> list[dict]:
        """读取签到历史（同步方法）。"""
        try:
            data = self.get_data("history")
            return list(data or [])
        except Exception:
            return []
